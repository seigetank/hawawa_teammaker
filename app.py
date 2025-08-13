# app.py
from flask import Flask, render_template, request
import itertools, random, urllib.parse, requests, datetime, time, uuid, sys, re, threading, os
from collections import defaultdict
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from nacl.signing import VerifyKey

app = Flask(__name__)

# --------------------------------
# 기본 설정
# --------------------------------
positions = ["탑", "정글", "미드", "원딜", "서폿"]

# GAS 릴레이 (Discord Webhook 프록시)
RELAY_BASE = 'https://script.google.com/macros/s/AKfycbyrVfddMN363ZhuBJH09es1MEmPC6lwhbyncXauc7I_Fh51GL7gJTx11bJdIddV7czmTg/exec'
RELAY_KEY  = 'hawawasiegetan'  # GAS SHARED_KEY
print(">>> LOADED: app.py")
# Discord Interactions 서명 검증 키
DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "").strip()

# 구글 시트
SHEET_NAME = "hawawa_db"
CREDS_FILE = "hawawa-teambuilder-e8bc633550b7.json"
SCORES_WS = "기록장"
PENDING_WS = "pending"

# 우리 서비스 기본 URL
BASE_URL = "https://seigetan.pythonanywhere.com"

# 속도 제한
LAST_SEND_TS = 0.0
MIN_INTERVAL = 1.0  # 초당 1건

# 웹 폼 투표(기존) 저장소
POLLS = {}

# 슬래시 익명 투표용 최신 세트(3개 조합)
CURRENT_POLL = {
    "options": [],        # ["1번","2번","3번"]
    "option_links": [],   # ["https://.../조합코드?a=..&b=..", ...]
    "votes": {},          # {user_id: idx}
    "created_at": 0
}
POLL_LOCK = threading.Lock()


# --------------------------------
# 입력 정규화 & 파싱(이름만)
# --------------------------------
def normalize_members_text(s: str) -> str:
    m = re.search(r'members\s*:\s*(.*)$', s, flags=re.I)
    if m: s = m.group(1)
    s = re.sub(r'^/?내전\s*', '', s).strip()
    tokens = [t.strip() for t in re.split(r'(?:\r\n|\r|\n|,|;|\||\u3000|\s{2,})+', s) if t.strip()]
    return "\n".join(tokens)

def parse_names_only(text: str):
    names = []
    for raw in text.strip().split("\n"):
        name = raw.strip()
        if name:
            names.append(name)
    return names


# --------------------------------
# Discord 서명 검증
# --------------------------------
def verify_discord_signature(req):
    if not DISCORD_PUBLIC_KEY:
        return False
    sig = request.headers.get("X-Signature-Ed25519", "")
    ts  = request.headers.get("X-Signature-Timestamp", "")
    body = request.get_data(as_text=False)
    try:
        vk = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        vk.verify(ts.encode() + body, bytes.fromhex(sig))
        return True
    except Exception:
        return False


# --------------------------------
# Google Sheets
# --------------------------------
def gs_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, scope)
    return gspread.authorize(creds)

def get_results_ws(client):
    return client.open(SHEET_NAME).sheet1

def get_or_create_pending_ws(client):
    ss = client.open(SHEET_NAME)
    try:
        ws = ss.worksheet(PENDING_WS)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=PENDING_WS, rows=1000, cols=3)
        ws.update('A1:C1', [['timestamp', 'link', 'status']])
    return ws

def pending_add(link):
    try:
        client = gs_client()
        ws = get_or_create_pending_ws(client)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([ts, link, "No"], value_input_option='USER_ENTERED')
    except Exception as e:
        print("⚠️ pending_add 실패:", e, file=sys.stderr)

def pending_mark_done_by_link(link):
    try:
        client = gs_client()
        ws = get_or_create_pending_ws(client)
        rows = ws.get_all_values()
        for i in range(2, len(rows)+1):
            row = rows[i-1]
            row_link = row[1] if len(row) > 1 else ""
            status   = row[2] if len(row) > 2 else ""
            if row_link == link and status != "Done":
                ws.update_cell(i, 3, "Done")
                return True
    except Exception as e:
        print("⚠️ pending_mark_done_by_link 실패:", e, file=sys.stderr)
    return False

def pending_fetch_unrecorded():
    links = []
    try:
        client = gs_client()
        ws = get_or_create_pending_ws(client)
        rows = ws.get_all_values()
        for r in rows[1:]:
            if len(r) >= 3 and r[2] != "Done" and r[1]:
                links.append(r[1])
    except Exception as e:
        print("⚠️ pending_fetch_unrecorded 실패:", e, file=sys.stderr)
    return links

def load_scores_map():
    try:
        client = gs_client()
        ws = client.open(SHEET_NAME).worksheet(SCORES_WS)
        rows = ws.get_all_values()
    except Exception as e:
        print("⚠️ load_scores_map 실패:", e, file=sys.stderr)
        return {}
    score_map = {}
    if not rows:
        return score_map
    for r in rows[1:]:
        if len(r) < 6:  # [이름,탑,정글,미드,원딜,서폿]
            continue
        name = (r[0] or "").strip()
        if not name:
            continue
        cols = r[1:6]
        scores = []
        for v in cols:
            v = (v or "").strip()
            try:
                scores.append(int(float(v)))
            except:
                scores.append(0)
        score_map[name] = scores
    return score_map


# --------------------------------
# 팀 배치/조합 계산
# --------------------------------
def valid_assignments(team, king_name=None, king_position=None):
    results = []
    king_index = None
    if king_name and king_position:
        for i, (name, _) in enumerate(team):
            if name == king_name:
                king_index = i
                break
    if king_name and king_index is None:
        return []
    if king_name:
        king_pos_index = positions.index(king_position)
        player_indices = list(range(5))
        pos_indices = list(range(5))
        player_indices.remove(king_index)
        pos_indices.remove(king_pos_index)
        for perm in itertools.permutations(pos_indices):
            total_score = 0
            assignment = []
            valid = True
            king_score = team[king_index][1][king_pos_index]
            if king_score <= 0:
                continue
            assignment.append((positions[king_pos_index], team[king_index][0], king_score))
            total_score += king_score
            for p_idx, pos in zip(player_indices, perm):
                name, scores = team[p_idx]
                score = scores[pos]
                if score <= 0:
                    valid = False
                    break
                assignment.append((positions[pos], name, score))
                total_score += score
            if valid:
                assignment = sorted(assignment, key=lambda x: positions.index(x[0]))
                results.append((total_score, assignment))
    else:
        for perm in itertools.permutations(range(5)):
            total_score = 0
            assignment = []
            valid = True
            for player, pos in zip(team, perm):
                name, scores = player
                score = scores[pos]
                if score <= 0:
                    valid = False
                    break
                assignment.append((positions[pos], name, score))
                total_score += score
            if valid:
                assignment = sorted(assignment, key=lambda x: positions.index(x[0]))
                results.append((total_score, assignment))
    return results


# --------------------------------
# Discord 송출 (GAS 릴레이)
# --------------------------------
def send_to_discord_text(content):
    global LAST_SEND_TS
    now = time.time()
    wait = MIN_INTERVAL - (now - LAST_SEND_TS)
    if wait > 0: time.sleep(wait)
    url = f"{RELAY_BASE}?key={RELAY_KEY}"
    payload = {"content": content}
    try:
        resp = requests.post(url, json=payload, timeout=12, headers={"Content-Type":"application/json"})
    except Exception as e:
        print(f"[RELAY] 예외: {repr(e)}", file=sys.stderr); return False
    print(f"[RELAY] status={resp.status_code} body[:200]={resp.text[:200]!r}", file=sys.stderr)
    if 200 <= resp.status_code < 300 and (resp.text or "").startswith("ok"):
        LAST_SEND_TS = time.time(); return True
    return False

def send_to_discord_json(obj):
    """
    GAS가 raw 포워딩을 지원하지 않아도, 내용은 반드시 나오게 폴백도 함께 보낸다.
    (버튼은 미표시)
    """
    global LAST_SEND_TS
    now = time.time()
    wait = MIN_INTERVAL - (now - LAST_SEND_TS)
    if wait > 0: time.sleep(wait)
    url = f"{RELAY_BASE}?key={RELAY_KEY}"

    # 1) 원래 시도 (GAS가 raw 지원하면 버튼 포함으로 보일 수 있음)
    try:
        resp = requests.post(url, json={"raw": obj}, timeout=12,
                             headers={"Content-Type": "application/json"})
        print(f"[RELAY-JSON] status={resp.status_code} body[:200]={resp.text[:200]!r}", file=sys.stderr)
        # 성공이든 아니든 타임스탬프만 업데이트
        LAST_SEND_TS = time.time()
    except Exception as e:
        print(f"[RELAY-JSON] 예외: {repr(e)}", file=sys.stderr)

    # 2) 무조건 텍스트 폴백도 보냄(중복 방지하려면 원한다면 환경변수로 제어)
    text = obj.get("content") if isinstance(obj, dict) else None
    if text:
        send_to_discord_text(text)
        return True
    return False


def send_long_to_discord(content):
    """
    Discord는 2000자 제한이 있으므로 길면 나눠서 전송
    """
    limit = 2000
    chunks = [content[i:i+limit] for i in range(0, len(content), limit)]
    for chunk in chunks:
        send_to_discord_text(chunk)  # 기존에 있는 Discord 전송 함수 사용

# --------------------------------
# 조합 송출 + CURRENT_POLL 저장
# --------------------------------
def send_to_discord_with_code(matches, title, raw_input_names):
    all_msgs, option_links = [], []
    for idx, (score_a, team_a, score_b, team_b) in enumerate(matches, 1):
        link = f"{BASE_URL}/조합코드?a={','.join([p[1] for p in team_a])}&b={','.join([p[1] for p in team_b])}"
        option_links.append(link)
        lines = [
            f"**{title} 조합 {idx}**",
            f"총합 A: {score_a} / 총합 B: {score_b}",
            "```",
            f"{'Team A':<25} | {'Team B':<25}",
            "-"*53
        ]
        for i in range(5):
            pa, na, sa = team_a[i]
            pb, nb, sb = team_b[i]
            lines.append(f"{pa}: {na} ({sa})".ljust(23) + " | " + f"{pb}: {nb} ({sb})")
        lines.append("```")
        all_msgs.append("\n".join(lines))
    send_long_to_discord("\n\n".join(all_msgs))

    labels = [f"{i}번" for i in range(1, len(option_links)+1)]
    vote_link, end_link = make_poll_links(f"{title} 전체 투표", labels, option_links)
    send_to_discord_text(f"/투표를 통해 투표를 하세요, 3분이지나거나 투표를 종료하려면 /공개 를 하세요 \n 비상용\n 🗳️ 웹투표: {vote_link}\n⏹️ 종료: {end_link}")

    with POLL_LOCK:
        CURRENT_POLL["options"] = labels[:]
        CURRENT_POLL["option_links"] = option_links[:]
        CURRENT_POLL["votes"].clear()
        CURRENT_POLL["created_at"] = int(time.time())


# --------------------------------
# 공개 메시지(기록 링크 + 기록담당 + 버튼)
# --------------------------------
def _parse_names_from_code_link(link: str):
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        a = [x.strip() for x in (qs.get("a", [""])[0] or "").split(",") if x.strip()]
        b = [x.strip() for x in (qs.get("b", [""])[0] or "").split(",") if x.strip()]
        return a, b
    except Exception:
        return [], []

def _publish_poll_snapshot_async(options, option_links, votes):
    try:
        # 집계
        counts = defaultdict(int)
        for _, idx in votes.items():
            if 0 <= idx < len(option_links):
                counts[idx] += 1
        total = len(option_links)
        tally = [counts[i] for i in range(total)]

        # 승자
        if not any(tally):
            winner_idx = None
            title = "**슬래시 투표 결과: 표가 없습니다**"
        else:
            m = max(tally)
            tops = [i for i, c in enumerate(tally) if c == m]
            winner_idx = random.choice(tops) if len(tops) > 1 else tops[0]
            tie = " (동점 → 랜덤 선정)" if len(tops) > 1 else ""
            title = f"**슬래시 투표 결과{tie}**"

        unresolved = pending_fetch_unrecorded()

        # 본문
        lines = [title, "```"]
        for i in range(total):
            lines.append(f"{i+1}번 : {tally[i]}")
        lines.append("```")

        if winner_idx is not None:
            picked = option_links[winner_idx]
            a_names, b_names = _parse_names_from_code_link(picked)
            ten = a_names + b_names if (len(a_names)==5 and len(b_names)==5) else []
            recorder = random.choice(ten) if ten else "기록담당(랜덤 실패)"
            lines.append(f"🧾 **승/패 기록 링크**: {picked}")
            lines.append(f"📝 **오늘의 기록담당**: {recorder}")
            # 버튼(components) — 1R, 2R
            components = [{
                "type": 1,
                "components": [
                    {"type":2, "style":3, "label":"1R A 승리", "custom_id": f"res|1|A|{urllib.parse.quote_plus(picked)}"},
                    {"type":2, "style":4, "label":"1R B 승리", "custom_id": f"res|1|B|{urllib.parse.quote_plus(picked)}"}
                ]
            }, {
                "type": 1,
                "components": [
                    {"type":2, "style":3, "label":"2R A 승리", "custom_id": f"res|2|A|{urllib.parse.quote_plus(picked)}"},
                    {"type":2, "style":4, "label":"2R B 승리", "custom_id": f"res|2|B|{urllib.parse.quote_plus(picked)}"},
                    {"type":2, "style":2, "label":"2R 진행 안 함", "custom_id": f"res|2|N|{urllib.parse.quote_plus(picked)}"}
                ]
            }]
            # 버튼 포함 송출 (GAS가 raw 그대로 webhook에 POST 하도록)
            send_to_discord_json({"content": "\n".join(lines), "components": components})
            pending_add(picked)
        else:
            send_long_to_discord("\n".join(lines))

        if unresolved:
            send_long_to_discord("⚠️ 이전에 승패가 기록되지 않은 조합:\n" + "\n".join(f"- {u}" for u in unresolved))
    except Exception as e:
        send_to_discord_text(f"⚠️ 공개 처리 실패: {e}")


# --------------------------------
# 내전 처리(백그라운드, 이름만)
# --------------------------------
def process_match_and_send(members_text: str, mode: str):
    try:
        names = parse_names_only(normalize_members_text(members_text))
        if len(names) != 10:
            send_to_discord_text("⚠️ 정확히 10명의 멤버를 입력하세요."); return
        scores_map = load_scores_map()
        missing = [n for n in names if n not in scores_map]
        if missing:
            send_to_discord_text(f"⚠️ 시트 '{SCORES_WS}'에서 점수를 찾지 못한 이름: {', '.join(missing)}"); return

        players = [(n, scores_map[n]) for n in names]
        exact, five = [], []
        for team_a in itertools.combinations(range(10), 5):
            team_b = [i for i in range(10) if i not in team_a]
            ta = [players[i] for i in team_a]
            tb = [players[i] for i in team_b]
            asg_a = valid_assignments(ta)
            asg_b = valid_assignments(tb)
            for sa, aa in asg_a:
                for sb, bb in asg_b:
                    d = abs(sa - sb)
                    if d == 0: exact.append((sa, aa, sb, bb))
                    elif d == 5: five.append((sa, aa, sb, bb))
        if mode == "exact": pool, title = exact, "0점 차이"
        elif mode == "five": pool, title = five, "5점 차이"
        else: pool, title = (exact + five), "전체 조합"

        if not pool:
            send_to_discord_text("조건을 만족하는 조합이 없습니다."); return

        picks = random.sample(pool, min(3, len(pool)))
        send_to_discord_with_code(picks, title, "\n".join(names))
    except Exception as e:
        send_to_discord_text(f"⚠️ 내전 처리 실패: {e}")

def send_to_discord_json(obj):
    """
    GAS가 raw 포워딩을 지원하지 않아도, 내용은 반드시 나오게 폴백도 함께 보낸다.
    (버튼은 미표시)
    """
    global LAST_SEND_TS
    now = time.time()
    wait = MIN_INTERVAL - (now - LAST_SEND_TS)
    if wait > 0: time.sleep(wait)
    url = f"{RELAY_BASE}?key={RELAY_KEY}"

    # 1) 원래 시도 (GAS가 raw 지원하면 버튼 포함으로 보일 수 있음)
    try:
        resp = requests.post(url, json={"raw": obj}, timeout=12,
                             headers={"Content-Type": "application/json"})
        print(f"[RELAY-JSON] status={resp.status_code} body[:200]={resp.text[:200]!r}", file=sys.stderr)
        # 성공이든 아니든 타임스탬프만 업데이트
        LAST_SEND_TS = time.time()
    except Exception as e:
        print(f"[RELAY-JSON] 예외: {repr(e)}", file=sys.stderr)

    # 2) 무조건 텍스트 폴백도 보냄(중복 방지하려면 원한다면 환경변수로 제어)
    text = obj.get("content") if isinstance(obj, dict) else None
    if text:
        send_to_discord_text(text)
        return True
    return False
def send_long_to_discord(content):
    """
    Discord는 2000자 제한이 있으므로 길면 나눠서 전송
    """
    limit = 2000
    chunks = [content[i:i+limit] for i in range(0, len(content), limit)]
    for chunk in chunks:
        send_to_discord_text(chunk)  # 기존에 있는 Discord 전송 함수 사용

# --------------------------------
# 웹 UI (이름만)
# --------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    default_input = """김희철
두낫
밈밈
소라다요
오곡
용
재재재
쿨라떼
찬욱
알리꼬"""
    if request.method == "POST":
        input_text = request.form.get("player_data", default_input)
        action = request.form.get("action")
        names = parse_names_only(input_text)
        if len(names) != 10:
            return render_template("index.html", error="⚠️ 정확히 10명의 플레이어를 입력해주세요.", default_input=input_text)
        scores_map = load_scores_map()
        missing = [n for n in names if n not in scores_map]
        if missing:
            return render_template("index.html",
                                   error=f"⚠️ 시트 '{SCORES_WS}'에서 점수를 찾지 못한 이름: {', '.join(missing)}",
                                   default_input=input_text, names=names, positions=positions)
        players = [(n, scores_map[n]) for n in names]
        exact, five = [], []
        for ta_idx in itertools.combinations(range(10), 5):
            tb_idx = [i for i in range(10) if i not in ta_idx]
            ta = [players[i] for i in ta_idx]; tb = [players[i] for i in tb_idx]
            asg_a = valid_assignments(ta); asg_b = valid_assignments(tb)
            for sa, aa in asg_a:
                for sb, bb in asg_b:
                    d = abs(sa - sb)
                    if d == 0: exact.append((sa, aa, sb, bb))
                    elif d == 5: five.append((sa, aa, sb, bb))
        if action == "random_exact":
            matches = random.sample(exact, min(3, len(exact)))
            send_to_discord_with_code(matches, "0점 차이", "\n".join(names))
            return render_template("index.html", result_type="random", matches=matches,
                                   default_input=input_text, names=names, positions=positions)
        elif action == "random_five":
            matches = random.sample(five, min(3, len(five)))
            send_to_discord_with_code(matches, "5점 차이", "\n".join(names))
            return render_template("index.html", result_type="random", matches=matches,
                                   default_input=input_text, names=names, positions=positions)
        elif action == "random_all":
            all_matches = exact + five
            matches = random.sample(all_matches, min(3, len(all_matches)))
            send_to_discord_with_code(matches, "전체 조합", "\n".join(names))
            return render_template("index.html", result_type="random", matches=matches,
                                   default_input=input_text, names=names, positions=positions)
    names = [line.strip() for line in default_input.strip().split('\n')]
    return render_template("index.html", default_input=default_input, names=names, positions=positions)


# --------------------------------
# 조합코드 (짧은 URL: a,b만)
# --------------------------------
@app.route("/조합코드")
def combination_code():
    a_csv = request.args.get("a", ""); b_csv = request.args.get("b", "")
    if not a_csv or not b_csv:
        return render_template("index.html", error="⚠️ 정보가 부족합니다.", default_input="")
    a_names = [n.strip() for n in a_csv.split(",") if n.strip()]
    b_names = [n.strip() for n in b_csv.split(",") if n.strip()]
    if len(a_names) != 5 or len(b_names) != 5:
        return render_template("index.html", error="⚠️ 팀 구성 실패(이름/인원/순서 확인)", default_input="")
    scores_map = load_scores_map()
    missing = [n for n in (a_names + b_names) if n not in scores_map]
    if missing:
        return render_template("index.html", error=f"⚠️ 시트에서 점수를 찾지 못한 이름: {', '.join(missing)}", default_input="")
    def build(names):
        team=[];
        for i, name in enumerate(names):
            scores = scores_map[name]
            team.append((positions[i], name, scores[i]))
        return team
    ta, tb = build(a_names), build(b_names)
    sa, sb = sum(x[2] for x in ta), sum(x[2] for x in tb)
    code_value = f"a={','.join(a_names)}&b={','.join(b_names)}"
    return render_template("index.html",
                           default_input="\n".join(a_names + b_names),
                           result_type="code",
                           code_match=(sa, ta, sb, tb),
                           code_value=code_value,
                           names=(a_names + b_names),
                           positions=positions)


# --------------------------------
# 결과 전송(구글 시트)
# --------------------------------
@app.route("/전송", methods=["POST"])
def submit_result():
    input_data = request.form.get("code_value")   # "a=...&b=..."
    a_team = request.form.get("a_team", "").split(",")
    b_team = request.form.get("b_team", "").split(",")
    winner1 = request.form.get("r1_result")       # "A"/"B"
    winner2 = request.form.get("r2_result")       # "A"/"B"/None
    if not input_data or not a_team or not b_team or not winner1:
        return "⚠️ 저장 실패: 정보 부족"
    now = datetime.datetime.now()
    date_str = f"{now.strftime('%y')}/{now.month}/{now.day}"
    rows = []
    if winner1 == "A": rows.append([date_str, "1RD"] + a_team + b_team)
    else:              rows.append([date_str, "1RD"] + b_team + a_team)
    if winner2 in ["A","B"]:
        if winner2 == "A": rows.append([date_str, "2RD"] + a_team + b_team)
        else:              rows.append([date_str, "2RD"] + b_team + a_team)
    try:
        client = gs_client(); sheet = get_results_ws(client)
        for r in rows: sheet.append_row(r, value_input_option='USER_ENTERED')
        combo_link = f"{BASE_URL}/조합코드?{input_data}"
        pending_mark_done_by_link(combo_link)
        return "✅ 저장 완료"
    except Exception as e:
        return f"⚠️ 저장 실패: {e}"


# --------------------------------
# 웹 폼 투표(기존)
# --------------------------------
def _make_poll(title, options, option_links):
    pid = uuid.uuid4().hex
    POLLS[pid] = {"title":title, "options":list(options), "option_links":list(option_links),
                  "votes":{}, "closed":False, "created_at":int(time.time())}
    return pid
def make_poll_links(title, options, option_links):
    pid = _make_poll(title, options, option_links)
    return f"{BASE_URL}/vote?pid={pid}", f"{BASE_URL}/vote/end?pid={pid}"

@app.route("/vote", methods=["GET","POST"])
def vote_page():
    pid = request.args.get("pid","")
    if not pid or pid not in POLLS: return "⚠️ 잘못된 링크입니다.", 400
    poll = POLLS[pid]
    if poll.get("closed"): return "⛔ 이미 종료된 투표입니다."
    if request.method=="POST":
        voter = (request.form.get("voter") or "").strip()
        choice = request.form.get("choice")
        if not voter or choice is None or not choice.isdigit(): return "⚠️ 닉네임과 선택지를 올바르게 입력하세요.", 400
        idx = int(choice)
        if idx<0 or idx>=len(poll["options"]): return "⚠️ 유효하지 않은 선택지입니다.", 400
        poll["votes"][voter]=idx
        return f"✅ {voter} 님의 투표가 저장되었습니다. (다시 투표하면 최신 표로 갱신됩니다)"
    opts = poll["options"]
    radios = "<br>".join([f'<label><input type="radio" name="choice" value="{i}" {"required" if i==0 else ""}> {label}</label>' for i,label in enumerate(opts)])
    return f"""<h2>🗳️ 투표 — {poll['title']}</h2>
    <form method="POST">닉네임: <input name="voter" required><div style="margin:10px 0;">{radios}</div><button type="submit">투표하기</button></form>"""

@app.route("/vote/end", methods=["GET"])
def vote_end_page():
    pid = request.args.get("pid","")
    if not pid or pid not in POLLS: return "⚠️ 잘못된 링크입니다.", 400
    poll = POLLS[pid]
    if poll.get("closed"): return "⛔ 이미 종료된 투표입니다."
    return f"""<h2>⏹️ 투표 종료 — {poll['title']}</h2>
    <p>정말 종료하시겠습니까? 종료 시 결과가 디스코드로 공지됩니다.</p>
    <form method="POST" action="/vote/end/confirm"><input type="hidden" name="pid" value="{pid}"><button type="submit">정말 종료</button></form>"""

@app.route("/vote/end/confirm", methods=["POST"])
def vote_end_confirm():
    pid = request.form.get("pid","")
    if not pid or pid not in POLLS: return "⚠️ 잘못된 요청입니다.", 400
    poll = POLLS[pid]
    if poll.get("closed"): return "⛔ 이미 종료된 투표입니다."
    unresolved = pending_fetch_unrecorded()
    counts = defaultdict(int)
    for _, idx in poll["votes"].items():
        if idx < len(poll["options"]): counts[idx]+=1
    if not counts:
        result_title = f"**투표 종료: {poll['title']}**\n(표가 없습니다)"; result_link=None; winner_idx=None
    else:
        maxv = max(counts.values()); tops=[i for i,c in counts.items() if c==maxv]
        winner_idx = random.choice(tops) if len(tops)>1 else tops[0]
        tie_note = " (동점 → 랜덤 선정)" if len(tops)>1 else ""
        result_title = f"**투표 종료: {poll['title']}**{tie_note}"
        result_link = poll["option_links"][winner_idx] if winner_idx is not None else None
    lines = [result_title, "```"]
    for i,label in enumerate(poll["options"]):
        lines.append(f"{label} : {counts.get(i,0)}")
    lines.append("```")
    if unresolved:
        lines.append("⚠️ 이전에 승패가 기록되지 않은 조합이 있습니다:")
        for link in unresolved: lines.append(f"- {link}")
    if result_link:
        a_names, b_names = _parse_names_from_code_link(result_link)
        ten = a_names + b_names if (len(a_names)==5 and len(b_names)==5) else []
        recorder = random.choice(ten) if ten else "기록담당(무작위 실패)"
        lines.append(f"🧾 **승/패 기록 링크**: {result_link}")
        lines.append(f"📝 **오늘의 기록담당**: {recorder}")
        pending_add(result_link)
    try:
        send_long_to_discord("\n".join(lines))
    except Exception as e:
        return f"⚠️ 디스코드 전송 실패: {e}", 500
    poll["closed"]=True
    return "✅ 결과를 디스코드로 공지했습니다. (이 페이지는 닫아도 됩니다)"


# --------------------------------
# 미기록 관리
# --------------------------------
@app.route("/pending", methods=["GET"])
def pending_list():
    links = pending_fetch_unrecorded()
    if not links: return "<h3>미기록 조합 없음</h3>"
    items = "".join([f"<li>{urllib.parse.quote(link, safe=':/?=&,')} <form style='display:inline' method='POST' action='/pending/resolve'><input type='hidden' name='link' value='{link}'><button type='submit'>해결</button></form></li>" for link in links])
    return f"<h3>미기록 조합 목록</h3><ul>{items}</ul>"

@app.route("/pending/resolve", methods=["POST"])
def pending_resolve():
    link = request.form.get("link","")
    if not link: return "⚠️ 링크 누락", 400
    ok = pending_mark_done_by_link(link)
    return "✅ 해결 처리" if ok else "⚠️ 처리 실패 또는 이미 해결됨"


# --------------------------------
# 테스트
# --------------------------------
@app.route("/webhook_test")
def webhook_test():
    txt = request.args.get("msg", "테스트 메시지 입니다.")
    ok = send_to_discord_text(f"[테스트] {txt}")
    return ("✅ 전송 성공" if ok else "❌ 전송 실패, error log 확인"), (200 if ok else 500)


# --------------------------------
# Discord Interactions (Slash & Buttons)
#  - /테스트핑
#  - /내전 members:<이름10줄> mode:<all|exact|five>
#  - /투표 choice:<1|2|3>
#  - /공개
#  - Buttons custom_id: res|<round:1|2>|<result:A|B|N>|<encoded_link>
# --------------------------------
@app.route("/discord", methods=["POST"])
def discord_interactions():
    if not verify_discord_signature(request):
        return ("invalid request signature", 401)
    payload = request.get_json(silent=True) or {}

    # PING
    if payload.get("type") == 1:
        return {"type": 1}

    # Application Command
    if payload.get("type") == 2:
        data = payload.get("data", {})
        cmd_name = (data.get("name") or "").strip().lower()

        # /테스트핑
        if cmd_name == "테스트핑":
            return {"type": 4, "data": {"content": "pong ✅ (PythonAnywhere OK)", "flags": 64}}

        # /내전
        if cmd_name == "내전":
            opts = {o.get("name"): o.get("value") for o in (data.get("options") or [])}
            members_text = (opts.get("members") or "").strip()
            mode = (opts.get("mode") or "all").lower()
            if not members_text:
                return {"type": 4, "data": {"content": "⚠️ 멤버 목록을 입력하세요.", "flags": 64}}
            try:
                threading.Thread(target=process_match_and_send, args=(members_text, mode), daemon=True).start()
            except Exception as e:
                return {"type": 4, "data": {"content": f"⚠️ 작업 시작 실패: {e}", "flags": 64}}
            return {"type": 4, "data": {"content": "⏳ 요청 접수! 곧 채널에 결과 올릴게요. ", "flags": 64}}

        # /투표 choice:<1|2|3>
        if cmd_name == "투표":
            opts = {o.get("name"): o.get("value") for o in (data.get("options") or [])}
            choice_raw = str(opts.get("choice", "")).strip()
            if choice_raw not in ["1","2","3"]:
                return {"type": 4, "data": {"content": "⚠️ choice는 1/2/3 중 하나여야 합니다.", "flags": 64}}
            idx = int(choice_raw) - 1
            with POLL_LOCK:
                if not CURRENT_POLL["option_links"]:
                    return {"type": 4, "data": {"content": "⚠️ 현재 투표 가능한 조합이 없습니다. 먼저 /내전 으로 생성하세요.", "flags": 64}}
                if idx < 0 or idx >= len(CURRENT_POLL["option_links"]):
                    return {"type": 4, "data": {"content": "⚠️ 유효하지 않은 선택지입니다.", "flags": 64}}
                # 사용자 ID로 익명 집계
                user_id = None
                if "member" in payload and payload["member"].get("user"):
                    user_id = payload["member"]["user"].get("id")
                if not user_id and payload.get("user"):
                    user_id = payload["user"].get("id")
                if not user_id:
                    user_id = uuid.uuid4().hex  # 최후의 수단(진짜 익명)
                CURRENT_POLL["votes"][user_id] = idx
            return {"type": 4, "data": {"content": f"✅ 투표 저장: {choice_raw}번", "flags": 64}}

        # /공개
        if cmd_name == "공개":
            with POLL_LOCK:
                if not CURRENT_POLL["option_links"]:
                    return {"type": 4, "data": {"content": "⚠️ 공개할 투표가 없습니다.", "flags": 64}}
                snap_options = CURRENT_POLL["options"][:]
                snap_links   = CURRENT_POLL["option_links"][:]
                snap_votes   = dict(CURRENT_POLL["votes"])
                CURRENT_POLL["options"].clear()
                CURRENT_POLL["option_links"].clear()
                CURRENT_POLL["votes"].clear()
                CURRENT_POLL["created_at"] = 0
            try:
                threading.Thread(target=_publish_poll_snapshot_async, args=(snap_options, snap_links, snap_votes), daemon=True).start()
            except Exception as e:
                return {"type": 4, "data": {"content": f"⚠️ 작업 시작 실패: {e}", "flags": 64}}
            return {"type": 4, "data": {"content": "📣 결과를 채널에 공개 중입니다!", "flags": 64}}

        return {"type": 4, "data": {"content": f"알 수 없는 명령: {cmd_name}", "flags": 64}}

    # Message Component(버튼)
    if payload.get("type") == 3:
        data = payload.get("data", {})
        custom_id = (data.get("custom_id") or "").strip()
        # 형식: res|<round:1|2>|<result:A|B|N>|<encoded_link>
        if custom_id.startswith("res|"):
            try:
                _, rnd, res, enc = custom_id.split("|", 3)
                link = urllib.parse.unquote_plus(enc)
                a_names, b_names = _parse_names_from_code_link(link)
                if len(a_names) != 5 or len(b_names) != 5:
                    return {"type": 4, "data": {"content": "⚠️ 링크 파싱 실패(팀 구성 오류).", "flags": 64}}
                # 시트 저장(1 or 2 라운드)
                now = datetime.datetime.now()
                date_str = f"{now.strftime('%y')}/{now.month}/{now.day}"
                rows = []
                if rnd == "1":
                    if res not in ["A","B"]:
                        return {"type": 4, "data": {"content": "⚠️ 1R 결과는 A/B만 가능합니다.", "flags": 64}}
                    rows.append([date_str, "1RD"] + (a_names + b_names if res=="A" else b_names + a_names))
                elif rnd == "2":
                    if res in ["A","B"]:
                        rows.append([date_str, "2RD"] + (a_names + b_names if res=="A" else b_names + a_names))
                    else:
                        # 진행 안 함(N) → 시트 미기록, 단순 확인만
                        pass
                else:
                    return {"type": 4, "data": {"content": "⚠️ 라운드 값 오류.", "flags": 64}}

                if rows:
                    try:
                        client = gs_client(); sheet = get_results_ws(client)
                        for r in rows: sheet.append_row(r, value_input_option='USER_ENTERED')
                        pending_mark_done_by_link(link)
                        return {"type": 4, "data": {"content": f"✅ {rnd}R 결과 저장 완료({res}).", "flags": 64}}
                    except Exception as e:
                        return {"type": 4, "data": {"content": f"⚠️ 저장 실패: {e}", "flags": 64}}
                else:
                    return {"type": 4, "data": {"content": "✅ 2R 진행 안 함 처리 완료(시트 기록 없음).", "flags": 64}}
            except Exception as e:
                return {"type": 4, "data": {"content": f"⚠️ 처리 오류: {e}", "flags": 64}}
        # 알 수 없는 버튼
        return {"type": 4, "data": {"content": "⚠️ 알 수 없는 버튼입니다.", "flags": 64}}

    return ("", 204)


# --------------------------------
# 로컬 디버그
# --------------------------------
if __name__ == "__main__":
    app.run(debug=True)

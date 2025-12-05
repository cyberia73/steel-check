# bot.py
import os
import json
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

# =========================
#      환경변수 로드
# =========================
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME")
MENTIONS_SHEET_NAME = os.getenv("MENTIONS_SHEET_NAME", "호출대상자")

_raw_alert_ids = os.getenv("ALERT_CHANNEL_ID", "")

# ALERT_CHANNEL_ID는 "채널ID1,채널ID2,..." 형식 (여러 채널 지원)
if _raw_alert_ids:
    ALERT_CHANNEL_IDS = []
    for cid in _raw_alert_ids.split(","):
        cid = cid.strip()
        if cid.isdigit():
            ALERT_CHANNEL_IDS.append(int(cid))
else:
    ALERT_CHANNEL_IDS = []

if not TOKEN:
    raise ValueError("DISCORD_TOKEN 환경변수가 설정되어 있지 않습니다.")

if not GOOGLE_CREDENTIALS_JSON:
    raise ValueError("GOOGLE_CREDENTIALS_JSON 환경변수가 설정되어 있지 않습니다.")

if not GOOGLE_SHEET_NAME or not GOOGLE_WORKSHEET_NAME:
    raise ValueError("GOOGLE_SHEET_NAME 또는 GOOGLE_WORKSHEET_NAME 이 설정되어 있지 않습니다.")

if not ALERT_CHANNEL_IDS:
    print("WARNING: ALERT_CHANNEL_ID 환경변수가 비어있거나 잘못되었습니다. 알림 채널이 없습니다.")

# =========================
#      디스코드 설정
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# =========================
#     구글 시트 인증
# =========================
creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)

sheet_file = gc.open(GOOGLE_SHEET_NAME)
timer_sheet = sheet_file.worksheet(GOOGLE_WORKSHEET_NAME)
mentions_sheet = sheet_file.worksheet(MENTIONS_SHEET_NAME)


# =========================
#      유틸 함수들
# =========================

def parse_datetime(dt_str: str) -> datetime | None:
    """시트에 저장된 날짜 문자열을 datetime으로 변환."""
    if not dt_str:
        return None
    dt_str = dt_str.strip()
    try:
        # "YYYY-MM-DDTHH:MM:SS" 또는 "YYYY-MM-DD HH:MM:SS"
        if "T" in dt_str:
            return datetime.fromisoformat(dt_str)
        else:
            return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def find_row(keyword: str) -> int | None:
    """
    시트 전체에서 keyword(예: '강철1')와 일치하는 셀을 찾고,
    그 셀이 속한 행 번호를 반환한다.

    - '강철1', '강철 1' 모두 허용 (공백 무시)
    - 어느 열에 있어도 상관 없음
    """
    data = timer_sheet.get_all_values()
    target = keyword.replace(" ", "")

    for row_idx, row in enumerate(data, start=1):
        for cell in row:
            val = (cell or "").replace(" ", "")
            if val == target:
                return row_idx
    return None


def get_timer_data(row: int):
    """
    해당 행의 타이머 정보를 반환.
    (name, start_dt, duration_sec, status, alert_stage)
    타이머가 없으면 None
    """
    values = timer_sheet.row_values(row)
    # 최소 5칸: 이름, 시작, 지속, 상태, 알람스테이지
    while len(values) < 5:
        values.append("")
    name = values[0]
    start_str = values[1]
    duration_str = values[2]
    status = values[3] or ""
    alert_stage = values[4] or "NONE"

    start_dt = parse_datetime(start_str)
    if not start_dt:
        return None

    try:
        duration = int(duration_str)
    except Exception:
        return None

    return name, start_dt, duration, status, alert_stage


def set_timer(row: int, duration_sec: int = 12 * 60 * 60):
    """
    새 타이머 시작: 현재 UTC 기준, duration_sec(기본 12시간)
    """
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    timer_sheet.update_cell(row, 2, now)           # 시작 시간
    timer_sheet.update_cell(row, 3, duration_sec)  # 지속(초)
    timer_sheet.update_cell(row, 4, "RUNNING")     # 상태
    timer_sheet.update_cell(row, 5, "NONE")        # 알람 스테이지


def mark_timer_done(row: int):
    """타이머를 종료 상태로 표시."""
    timer_sheet.update_cell(row, 4, "DONE")
    timer_sheet.update_cell(row, 5, "DONE")


def update_alert_stage(row: int, stage: str):
    """알람 스테이지 업데이트 (NONE, 4H, 2H, 1H, 30M, DONE 등)."""
    timer_sheet.update_cell(row, 5, stage)


def get_steel_mentions() -> list[int]:
    """
    호출대상자 시트에서 '강철대상자' 행의 대상자 ID들을 읽어옴.
    A2: "강철대상자"
    B2 ~ : 디스코드 user_id 문자열
    """
    row_values = mentions_sheet.row_values(2)  # 2행 전체
    ids: list[int] = []
    # B열부터 끝까지
    for val in row_values[1:]:
        val = (val or "").strip()
        if not val:
            continue
        if val.isdigit():
            ids.append(int(val))
    return ids


async def broadcast_alert(message: str):
    """
    ALERT_CHANNEL_IDS에 설정된 모든 채널에 동일한 메시지 전송.
    """
    if not ALERT_CHANNEL_IDS:
        print("ERROR: Alert channel list is empty.")
        return

    for cid in ALERT_CHANNEL_IDS:
        channel = bot.get_channel(cid)
        if channel:
            try:
                await channel.send(message)
            except Exception as e:
                print(f"ERROR sending message to channel {cid}: {e}")


def format_mentions_for_steel() -> str:
    """
    강철 대상자 멘션 문자열 생성: "<@id1> <@id2> ..."
    대상자가 없으면 빈 문자열.
    """
    ids = get_steel_mentions()
    if not ids:
        return ""
    return " " + " ".join(f"<@{uid}>" for uid in ids)


# =========================
#     타이머 백그라운드
# =========================

@tasks.loop(seconds=150)  # 150초마다 체크
async def timer_checker():
    now = datetime.utcnow()

    # 시트 전체 읽기
    data = timer_sheet.get_all_values()
    # 1행은 헤더라고 가정, 2행부터 타이머 데이터
    for row_idx, row in enumerate(data[1:], start=2):
        # 최소 5칸 확보
        while len(row) < 5:
            row.append("")

        name = row[0]
        start_str = row[1]
        duration_str = row[2]
        status = row[3] or ""
        alert_stage = row[4] or "NONE"

        if status != "RUNNING":
            continue

        start_dt = parse_datetime(start_str)
        if not start_dt:
            continue

        try:
            duration = int(duration_str)
        except Exception:
            continue

        end_time = start_dt + timedelta(seconds=duration)
        left_sec = int((end_time - now).total_seconds())

        # 이미 끝난 경우
        if left_sec <= 0:
            # 종료 알림 (이미 DONE 처리된 것이라면 스킵)
            if status == "RUNNING":
                mentions = format_mentions_for_steel()
                msg = f"⏰ **{name} 타이머 종료!**{mentions}"
                await broadcast_alert(msg)
                mark_timer_done(row_idx)
            continue

        # 남은 시간 기준 알림들
        # 4시간(14400), 2시간(7200), 1시간(3600), 30분(1800)
        # 이미 지난 스테이지는 건너뛰고,
        # 재시작 후 처음 체크 시점에도 조건 만족하면 바로 울리도록 설계
        def stage_allowed(prev: str, current: str) -> bool:
            order = ["NONE", "4H", "2H", "1H", "30M", "DONE"]
            try:
                return order.index(prev) < order.index(current)
            except ValueError:
                # 이상한 값이면 그냥 통과시켜버림 (안전)
                return True

        # 4시간 전
        if left_sec <= 4 * 3600 and left_sec > 2 * 3600 and stage_allowed(alert_stage, "4H"):
            mentions = format_mentions_for_steel()
            msg = f"⏳ **{name} 타이머 4시간 전입니다!**{mentions}"
            await broadcast_alert(msg)
            update_alert_stage(row_idx, "4H")
            alert_stage = "4H"

        # 2시간 전
        if left_sec <= 2 * 3600 and left_sec > 1 * 3600 and stage_allowed(alert_stage, "2H"):
            mentions = format_mentions_for_steel()
            msg = f"⏳ **{name} 타이머 2시간 전입니다!**{mentions}"
            await broadcast_alert(msg)
            update_alert_stage(row_idx, "2H")
            alert_stage = "2H"

        # 1시간 전
        if left_sec <= 1 * 3600 and left_sec > 30 * 60 and stage_allowed(alert_stage, "1H"):
            mentions = format_mentions_for_steel()
            msg = f"⏳ **{name} 타이머 1시간 전입니다!**{mentions}"
            await broadcast_alert(msg)
            update_alert_stage(row_idx, "1H")
            alert_stage = "1H"

        # 30분 전
        if left_sec <= 30 * 60 and stage_allowed(alert_stage, "30M"):
            mentions = format_mentions_for_steel()
            msg = f"⏳ **{name} 타이머 30분 전입니다!**{mentions}"
            await broadcast_alert(msg)
            update_alert_stage(row_idx, "30M")
            alert_stage = "30M"


# =========================
#        명령어들
# =========================

@bot.command(name="강철")
async def 강철(ctx: commands.Context, number: str):
    """
    !강철 X
    - 시트에 '강철X'가 없으면: 새 행 생성 후 12시간 타이머 시작
    - 시트에 이미 있으면:
        * RUNNING이면 남은 시간 표시
        * 그 외면 새 12시간 타이머 다시 시작
    """
    key = f"강철{number}"

    # 1) 먼저 기존 행을 찾는다
    row = find_row(key)

    # 2) 없으면 시트 맨 아래에 새 행 만들고 타이머 시작
    if not row:
        data = timer_sheet.get_all_values()
        row = len(data) + 1  # 맨 마지막 다음 줄

        # A열에 이름만 먼저 써 둔다
        timer_sheet.update_cell(row, 1, key)

        # 새 타이머 시작
        set_timer(row, duration_sec=12 * 60 * 60)
        await ctx.send(f"⏳ **{key} 타이머가 시트에 새로 생성되고, 12시간 타이머를 시작했습니다.**")
        return

    # 3) 기존 행이 있는 경우: 그 행의 타이머 상태를 본다
    timer = get_timer_data(row)

    # 타이머 정보가 없거나(이전에 깨끗이 비워진 상태), RUNNING이 아니면 새로 시작
    if not timer:
        set_timer(row, duration_sec=12 * 60 * 60)
        await ctx.send(f"⏳ **{key} 타이머를 새로 시작했습니다! (12시간)**")
        return

    name, start_dt, duration, status, alert_stage = timer

    if status == "RUNNING":
        # 남은 시간 계산
        end_time = start_dt + timedelta(seconds=duration)
        left = end_time - datetime.utcnow()
        sec = int(left.total_seconds())
        if sec <= 0:
            await ctx.send(f"🔔 {key} 타이머는 이미 종료되었습니다.")
            return
        h, m = divmod(sec // 60, 60)
        s = sec % 60
        await ctx.send(f"🕒 **{key} 남은 시간:** {h}시간 {m}분 {s}초")
        return
    else:
        # RUNNING이 아니면(예: DONE) 새 타이머 다시 시작
        set_timer(row, duration_sec=12 * 60 * 60)
        await ctx.send(f"⏳ **{key} 타이머를 다시 시작했습니다! (12시간)**")


@bot.command(name="완료")
async def 완료(ctx: commands.Context, kind: str, number: str):
    """
    !완료 강철 X
    - 해당 강철 X 타이머를 강제 종료(DONE) 처리
    """
    if kind != "강철":
        await ctx.send("지금은 '강철' 타이머만 완료 처리할 수 있습니다. 예: `!완료 강철 1`")
        return

    key = f"강철{number}"
    row = find_row(key)
    if not row:
        await ctx.send("시트에서 해당 강철 번호를 찾을 수 없습니다.")
        return

    timer = get_timer_data(row)
    if not timer:
        await ctx.send(f"{key} 타이머는 시작된 기록이 없습니다.")
        return

    name, start_dt, duration, status, alert_stage = timer
    if status != "RUNNING":
        await ctx.send(f"{key} 타이머는 이미 완료된 상태입니다.")
        return

    mark_timer_done(row)
    await ctx.send(f"✅ **{key} 타이머를 수동으로 완료 처리했습니다.**")


@bot.command(name="강철대상")
async def 강철대상(ctx: commands.Context):
    """
    !강철대상 @사람1 @사람2 ...
    - 호출대상자 시트의 강철 대상자 목록(B2~)에 추가
    """
    if not ctx.message.mentions:
        await ctx.send("추가할 대상을 멘션해주세요. 예: `!강철대상 @사용자`")
        return

    # 2행 전체 읽기
    row_vals = mentions_sheet.row_values(2)
    # 최소 2칸 이상 확보
    while len(row_vals) < 2:
        row_vals.append("")

    existing_ids = set((v or "").strip() for v in row_vals[1:] if (v or "").strip())

    added = []
    for member in ctx.message.mentions:
        uid_str = str(member.id)
        if uid_str not in existing_ids:
            # 첫 빈 칸 찾기 (B열부터)
            # row_vals[0] = A2, row_vals[1] = B2 ...
            try:
                first_empty_idx = next(
                    i for i, v in enumerate(row_vals[1:], start=2) if not (v or "").strip()
                )
            except StopIteration:
                # 빈 칸이 없으면 맨 끝 다음 칸에 추가
                first_empty_idx = len(row_vals) + 1
            mentions_sheet.update_cell(2, first_empty_idx, uid_str)
            existing_ids.add(uid_str)
            added.append(member.mention)

    if added:
        await ctx.send(f"강철 알림 대상에 추가: {', '.join(added)}")
    else:
        await ctx.send("추가할 신규 대상이 없습니다.")


@bot.command(name="강철대상제외")
async def 강철대상제외(ctx: commands.Context):
    """
    !강철대상제외 @사람1 @사람2 ...
    - 호출대상자 시트의 강철 대상자 목록에서 제거
    """
    if not ctx.message.mentions:
        await ctx.send("제외할 대상을 멘션해주세요. 예: `!강철대상제외 @사용자`")
        return

    row_vals = mentions_sheet.row_values(2)
    while len(row_vals) < 2:
        row_vals.append("")

    removed = []
    for member in ctx.message.mentions:
        uid_str = str(member.id)
        # B열부터 검사
        for col_idx in range(2, len(row_vals) + 1):
            cell_val = mentions_sheet.cell(2, col_idx).value or ""
            if cell_val.strip() == uid_str:
                mentions_sheet.update_cell(2, col_idx, "")
                removed.append(member.mention)
                break

    if removed:
        await ctx.send(f"강철 알림 대상에서 제외: {', '.join(removed)}")
    else:
        await ctx.send("제외할 대상이 목록에 없습니다.")


# =========================
#        봇 준비 이벤트
# =========================
@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user}")
    if not timer_checker.is_running():
        timer_checker.start()


# =========================
#          실행
# =========================
bot.run(TOKEN)

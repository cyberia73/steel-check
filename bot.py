import discord
from discord.ext import commands, tasks
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import json
import os
from dotenv import load_dotenv

# --------------------------------
# .env 로드
# --------------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME")
MENTIONS_SHEET_NAME = os.getenv("MENTIONS_SHEET_NAME")

# ALERT_CHANNEL_ID 안전 처리 + 디버그 출력
_raw_alert_id = os.getenv("ALERT_CHANNEL_ID")
if not _raw_alert_id:
    print("WARNING: ALERT_CHANNEL_ID 환경변수가 설정되어 있지 않습니다.")
    ALERT_CHANNEL_ID = None
else:
    try:
        ALERT_CHANNEL_ID = int(_raw_alert_id)
    except ValueError:
        print(f"WARNING: ALERT_CHANNEL_ID 값이 잘못되었습니다: {_raw_alert_id!r}")
        ALERT_CHANNEL_ID = None

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# 12시간(초)
DURATION_SECONDS = 12 * 3600

# --------------------------------
# 구글 인증
# --------------------------------
creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)

scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)

sheet_file = gc.open(GOOGLE_SHEET_NAME)
ws = sheet_file.worksheet(GOOGLE_WORKSHEET_NAME)
mention_ws = sheet_file.worksheet(MENTIONS_SHEET_NAME)

# --------------------------------
# 디스코드 봇 설정
# --------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --------------------------------
# 유틸 함수
# --------------------------------

def find_row(material_name: str):
    """이름(예: 강철1)에 해당하는 행 번호 찾기"""
    try:
        cell = ws.find(material_name)
        return cell.row
    except Exception:
        return None


def get_steel_mentions():
    """
    호출대상자 시트 2행(B2~)에서 강철 알림 대상자 리스트 가져오기
    A2 = "강철대상자"
    B2~ = @유저 들
    """
    row = mention_ws.row_values(2)[1:]  # B2~
    return [x for x in row if x]


def parse_start_time(value: str):
    """
    시트에 저장된 시간 문자열을 datetime으로 변환.
    지원:
      - 2025-12-05T07:35:09
      - 2025-12-05 7:35:09 (공백 -> T로 변환해서 처리)
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            cleaned = value.replace(" ", "T")
            return datetime.fromisoformat(cleaned)
        except Exception:
            return None


def format_remaining(remain_sec: float) -> str:
    sec = int(remain_sec)
    if sec < 0:
        sec = 0
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h}시간 {m}분 {s}초"


# --------------------------------
# 명령어
# --------------------------------

@bot.command(name="강철")
async def steel_timer(ctx, number: int):
    """
    !강철 X
      - 행 있으면 남은 시간 표시
      - 없거나 끝났으면 12시간 타이머 새로 시작
    """
    name = f"강철{number}"
    row = find_row(name)

    # 행이 없으면 새로 생성
    if row is None:
        ws.append_row([name, "", "", "", "0"])
        row = find_row(name)

    start_value = ws.cell(row, 2).value

    if start_value:
        start_dt = parse_start_time(start_value)
        if start_dt:
            elapsed = (datetime.now() - start_dt).total_seconds()
            if elapsed < DURATION_SECONDS:
                remain = DURATION_SECONDS - elapsed
                await ctx.send(
                    f"⏳ **[{name}] 남은 시간:** {format_remaining(remain)}"
                )
                return

    # 새 12시간 타이머 시작
    ws.update_cell(row, 2, datetime.now().isoformat())  # 시작시간
    ws.update_cell(row, 3, DURATION_SECONDS)            # duration
    ws.update_cell(row, 5, "0")                         # 알람 단계 초기화

    await ctx.send(f"🔔 **[{name}] 타이머 시작 (12시간)**")


@bot.command(name="완료")
async def finish_timer(ctx, mat: str, number: int):
    """
    !완료 강철 X
      - 강철X 행 자체를 시트에서 삭제
    """
    if mat != "강철":
        await ctx.send("현재는 **강철만 지원**합니다.")
        return

    name = f"강철{number}"
    row = find_row(name)

    if row is None:
        await ctx.send(f"❌ [{name}] 항목이 없습니다.")
        return

    ws.delete_rows(row)
    await ctx.send(f"🧹 **[{name}] 타이머 삭제 완료.**")


@bot.command(name="강철대상")
async def add_steel_target(ctx, *members):
    """
    !강철대상 @유저1 @유저2 ...
      - 호출대상자 시트 2행(B2~)에 대상자 추가
    """
    if not members:
        await ctx.send("추가할 멤버를 멘션해주세요.\n예: `!강철대상 @유저`")
        return

    row = mention_ws.row_values(2)[1:]  # B2~
    updated = list(row)

    added = []
    for m in members:
        if m not in updated:
            updated.append(m)
            added.append(m)

    if updated:
        end_col_letter = chr(65 + len(updated))  # A=65
        mention_ws.update(f"B2:{end_col_letter}2", [updated])

    if added:
        await ctx.send(f"✅ 추가됨: {', '.join(added)}")
    else:
        await ctx.send("추가된 대상이 없습니다. (이미 모두 포함되어 있음)")


@bot.command(name="강철대상제외")
async def remove_steel_target(ctx, *members):
    """
    !강철대상제외 @유저1 @유저2 ...
      - 호출대상자 시트 2행(B2~)에서 대상자 제거
    """
    if not members:
        await ctx.send("제거할 멤버를 멘션해주세요.\n예: `!강철대상제외 @유저`")
        return

    row = mention_ws.row_values(2)[1:]
    updated = [x for x in row if x not in members]

    if updated:
        end_col_letter = chr(65 + len(updated))
        mention_ws.update(f"B2:{end_col_letter}2", [updated])
    else:
        # 모두 제거되면 B2~Z2 빈칸으로 초기화
        mention_ws.update("B2:Z2", [[""] * 25])

    await ctx.send(f"🗑 제거됨: {', '.join(members)}")


# --------------------------------
# 타이머 체크 루프 (150초마다 1번)
# --------------------------------

@tasks.loop(seconds=150)
async def timer_check():
    """
    모든 강철 타이머를 150초(2.5분)마다 체크해서
    4시간 / 2시간 / 1시간 / 30분 / 종료 알람을 보냄.
    """
    if ALERT_CHANNEL_ID is None:
        print("ERROR: ALERT_CHANNEL_ID가 설정되어 있지 않아 알림 채널을 찾을 수 없습니다.")
        return

    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if channel is None:
        print(f"ERROR: Alert channel (ID={ALERT_CHANNEL_ID}) not found.")
        return

    try:
        all_rows = ws.get_all_values()
    except Exception as e:
        print(f"ERROR: failed to read sheet: {e}")
        return

    for i, row in enumerate(all_rows[1:], start=2):
        # 최소한 이름/시작시간 정도는 있어야 의미 있음
        if not row or len(row) < 2:
            continue

        name = row[0]
        start_val = row[1]

        if not start_val:
            continue

        # 알람 단계(stage) 읽기 (이상한 값이면 0)
        stage = 0
        if len(row) >= 5:
            raw = (row[4] or "").strip().upper()
            if raw not in ["", "NONE", "NULL", "N/A"]:
                try:
                    stage = int(raw)
                except ValueError:
                    stage = 0

        start_dt = parse_start_time(start_val)
        if not start_dt:
            # 시간을 못 읽으면 이 행은 건너뜀
            continue

        elapsed = (datetime.now() - start_dt).total_seconds()
        remain = DURATION_SECONDS - elapsed

        mentions = get_steel_mentions()
        mention_text = " ".join(mentions) if mentions else ""

        # 0 이하 -> 종료 알람
        if remain <= 0 and stage < 5:
            await channel.send(
                f"{mention_text}\n"
                f"⏰ **[{name}] 타이머 종료!**"
            )
            ws.update_cell(i, 5, "5")
            continue

        # 남은 시간에 따른 알람들 (4h / 2h / 1h / 30m)
        alerts = [
            (4 * 3600, 1, "4시간 남았습니다!"),
            (2 * 3600, 2, "2시간 남았습니다!"),
            (1 * 3600, 3, "1시간 남았습니다!"),
            (30 * 60,  4, "30분 남았습니다!"),
        ]

        for threshold, new_stage, msg in alerts:
            # remain이 threshold 이하로 떨어지고, 아직 해당 단계 이전이면 울림
            if remain <= threshold and stage < new_stage:
                await channel.send(
                    f"{mention_text}\n"
                    f"🔔 **[{name}] {msg}**"
                )
                ws.update_cell(i, 5, str(new_stage))
                break


@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user}")
    if not timer_check.is_running():
        timer_check.start()


bot.run(DISCORD_TOKEN)

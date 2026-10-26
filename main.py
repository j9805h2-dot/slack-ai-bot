import os
import re
import csv
import io
import json
import time
import urllib.parse
from collections import OrderedDict
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from slack_sdk import WebClient
from google import genai
from google.genai import types

app = FastAPI()

# 1. 환경변수 로드
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
raw_keys = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
    os.environ.get("GEMINI_API_KEY"),
]
GEMINI_API_KEYS = list(dict.fromkeys([k.strip() for k in raw_keys if k and k.strip()]))
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# 슬랙 3초 타임아웃 재전송(Retry)으로 인한 중복 응답 방지 캐시
PROCESSED_EVENTS = OrderedDict()
KEY_INDEX = 0  # API 키 라운드로빈 분산용 (429 에러 완벽 방지)

# 2. 마스터 지식 시트 캐싱 (1,347건)
MASTER_SHEET_ID = os.environ.get("MASTER_SHEET_ID", "1k-eGv2EFu4GG3H3oKG-Ql9IF-UVD_kZMTjnUiyLqLp4")
CSV_EXPORT_URL = f"https://docs.google.com/spreadsheets/d/{MASTER_SHEET_ID}/gviz/tq?tqx=out:csv&sheet=%F0%9F%93%9AAI_%EB%A7%88%EC%8A%A4%ED%84%B0%EC%A7%80%EC%8B%9D"

CACHED_KNOWLEDGE = []

def sync_master_knowledge():
    global CACHED_KNOWLEDGE
    try:
        res = requests.get(CSV_EXPORT_URL, timeout=15)
        if res.status_code == 200:
            f = io.StringIO(res.text)
            reader = csv.reader(f)
            rows = list(reader)
            
            new_data = []
            for r in rows[1:]:
                if len(r) >= 4:
                    cat = r[0].strip() if len(r) > 0 else ""
                    sub = r[1].strip() if len(r) > 1 else ""
                    q = r[2].strip() if len(r) > 2 else ""
                    a = r[3].strip() if len(r) > 3 else ""
                    kw = r[4].strip() if len(r) > 4 else ""

                    if q and a:
                        kw_list = [k.strip().lower() for k in kw.split(",") if k.strip()]
                        kw_list.extend(re.findall(r"[\w가-힣]+", q.lower()))
                        kw_list.extend(re.findall(r"[\w가-힣]+", sub.lower()))
                        new_data.append({
                            "category": cat,
                            "sub": sub,
                            "question": q,
                            "answer": a,
                            "keywords": list(set(kw_list))
                        })

            if new_data:
                CACHED_KNOWLEDGE = new_data
                print(f"✅ 마스터 지식 로드 성공: 총 {len(CACHED_KNOWLEDGE)}건 캐싱 완료")
                return len(CACHED_KNOWLEDGE)
    except Exception as e:
        print(f"⚠️ 시트 로드 예외: {e}")
    return len(CACHED_KNOWLEDGE)

sync_master_knowledge()

# 3. 실무 도메인별 엄격 분리 동의어 사전 (해약 vs 청약철회 vs 계약취소 혼동 원천 차단!)
SYNONYM_MAP = {
    # [구분 1] 해약/해지 (정식 계약 후 중도해약 - 구비서류 및 환급금 관련)
    "해약": ["해지", "중도해약", "해약서류", "구비서류", "환급금", "해약환급금"],
    "해지": ["해약", "중도해약", "해약서류", "구비서류", "환급금"],
    
    # [구분 2] 청약철회 (14일 이내 - 별도 서류 불필요, 철회 녹취)
    "철회": ["청약철회", "14일", "철회녹취"],
    "청약철회": ["철회", "14일", "철회녹취"],
    
    # [구분 3] 계약취소 (임시계약 상태 - 별도 서류 및 녹취 불필요, 요청 시 즉시 처리)
    "계약취소": ["임시계약", "취소", "청약취소", "접수취소"],
    "임시계약": ["계약취소", "취소", "결제전"],

    # [기타 주요 실무 키워드]
    "장기연체": ["연체", "미납", "7회", "출금", "재출금", "직권해지"],
    "연체": ["미납", "출금", "재출금", "납부"],
    "미납": ["연체", "출금", "재출금", "납부"],
    "서류": ["구비서류", "신분증", "초본", "확인서", "대체서류"],
    "귀화": ["외국인", "국적", "초본", "귀화확인서", "정보변경"],
    "대납": ["대리납부", "납부자변경", "타인납부", "직계가족"],
    "대리납부": ["대납", "납부자변경", "직계가족", "동의녹취"],
}

def extract_core_stems(text: str):
    """조사 및 어미 분리 + 도메인별 정확한 동의어 확장"""
    clean = re.sub(r"[^\w\s가-힣]", " ", text.lower())
    words = clean.split()
    stems = set()
    
    stop_words = {"알려줘", "뭐야", "뭐가", "어떻게", "언제", "얼마", "확인해줘", "답변", "부탁", "경우", "경우는", "관련", "그럼", "만약", "필요해", "하나요", "할까요", "있나요"}
    josa_pattern = r"(시에는|일때는|할때는|경우에는|경우는|에서는|이라도|시엔|할때|에는|에서|에게|으로|까지|부터|마다|이면|면|의|과|와|을|를|은|는|이|가|도|만|시|해|요|죠)$"

    for w in words:
        if w in stop_words or len(w) < 2:
            continue
        stems.add(w)
        stripped = re.sub(josa_pattern, "", w)
        if len(stripped) >= 2 and stripped != w and stripped not in stop_words:
            stems.add(stripped)

    # 동의어 확장 적용 (정확히 일치하거나 핵심어가 포함된 경우만 안전하게 확장)
    expanded = set(stems)
    for s in list(stems):
        if s in SYNONYM_MAP:
            expanded.update(SYNONYM_MAP[s])

    return list(expanded)

def get_thread_context(channel_id: str, thread_ts: str, current_ts: str):
    """스레드 내 이전 대화 맥락(질문+봇 답변 핵심)을 가져와 후속 질문 완벽 이해"""
    if not thread_ts or thread_ts == current_ts:
        return "", ""
    try:
        res = slack_client.conversations_replies(channel=channel_id, ts=thread_ts, limit=6)
        msgs = res.get("messages", [])
        if len(msgs) <= 1:
            return "", ""
        
        history_lines = []
        user_keywords_source = []
        for m in msgs[:-1][-4:]:  # 직전 4개 메시지만 참조하여 토큰 절약
            is_bot = bool(m.get("bot_id"))
            role = "대성봇" if is_bot else "상담사"
            txt = re.sub(r"\[질의-AI\]", "", m.get("text", "")).strip()
            if not is_bot:
                user_keywords_source.append(txt)
            short_txt = txt[:250].replace("\n", " ") + ("..." if len(txt) > 250 else "")
            history_lines.append(f"- {role}: {short_txt}")
            
        return "\n".join(history_lines), " ".join(user_keywords_source)
    except Exception as e:
        print(f"⚠️ 스레드 맥락 조회 실패: {e}")
        return "", ""

def search_memory_knowledge(current_query: str, prev_user_queries: str = ""):
    """
    [고도화 색인 엔진]
    현재 질문에 높은 가중치를 두고, 스레드 후속 질문일 경우 이전 질문의 맥락까지 결합하여
    관련된 상위 5개 지식 조각(페이지 분할 문서 포함, 약 1,500 토큰 내외)을 정확히 추출
    """
    current_stems = extract_core_stems(current_query)
    prev_stems = extract_core_stems(prev_user_queries) if prev_user_queries else []
    clean_q = re.sub(r"[^\w\s가-힣]", "", current_query.lower())

    scored = []
    for item in CACHED_KNOWLEDGE:
        score = 0
        sub_clean = re.sub(r"[^\w\s가-힣]", "", item["sub"].lower())
        q_clean = re.sub(r"[^\w\s가-힣]", "", item["question"].lower())
        ans_clean = item["answer"].lower()

        # 1. 현재 질문 핵심어 매칭 (주 가중치)
        for stem in current_stems:
            if stem in sub_clean:
                score += 120
            if stem in q_clean:
                score += 80
            for kw in item["keywords"]:
                if stem == kw:
                    score += 60
                elif stem in kw or kw in stem:
                    score += 25
            if stem in ans_clean:
                score += 15

        # 2. 스레드 이전 질문 핵심어 매칭 (보조 가중치 - 꼬리질문 해결)
        for p_stem in prev_stems:
            if p_stem not in current_stems:
                if p_stem in sub_clean:
                    score += 45
                if p_stem in q_clean:
                    score += 30
                if p_stem in ans_clean:
                    score += 10

        # 3. 질문 통째 부분 일치 보너스
        if len(clean_q) >= 2 and (clean_q in sub_clean or clean_q in q_clean):
            score += 100

        if score >= 25:
            scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    
    # 상위 5개 조각을 전달하되, 각 조각당 최대 700자로 제한하여 429 토큰 초과 원천 차단!
    if scored:
        top_items = [s[1] for s in scored[:5]]
        combined_text = "\n\n---\n\n".join([
            f"[{it['category']} > {it['sub']} | {it['question']}]\n{it['answer'][:700]}"
            for it in top_items
        ])
        return combined_text, True, top_items[0]
    return "", False, None

def format_for_slack(text: str) -> str:
    """AI가 습관적으로 출력하는 **볼드체**를 슬랙 전용 *볼드체*로 완벽 변환"""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"^#{1,3}\s*(.+)$", r"*\1*", text, flags=re.MULTILINE)
    return text.strip()

def call_gemini_api(prompt: str):
    """
    [429 에러 완벽 방어]
    1) 실제 존재하는 최신 정식 모델명(gemini-2.5-flash, gemini-2.0-flash) 사용
    2) 여러 개의 API 키를 순차적으로 돌아가며(Round-Robin) 호출하여 키 1개에 부하 집중 방지
    """
    global KEY_INDEX
    if not GEMINI_API_KEYS:
        return False, ""

    models = ["gemini-2.5-flash", "gemini-2.0-flash"]
    num_keys = len(GEMINI_API_KEYS)

    for i in range(num_keys):
        current_key = GEMINI_API_KEYS[(KEY_INDEX + i) % num_keys]
        try:
            client = genai.Client(api_key=current_key)
            for m in models:
                try:
                    res = client.models.generate_content(
                        model=m,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=0.15,
                            max_output_tokens=1500
                        )
                    )
                    if res and res.text:
                        KEY_INDEX = (KEY_INDEX + i + 1) % num_keys
                        return True, format_for_slack(res.text)
                except Exception as model_err:
                    err_str = str(model_err)
                    print(f"⚠️ 모델 호출 경고 ({m}): {err_str[:100]}")
                    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        time.sleep(1.0)
                        break  # 현재 키가 429면 즉시 다음 API 키로 전환!
                    continue
        except Exception as key_err:
            print(f"⚠️ API 키 초기화 오류: {key_err}")
            continue
    return False, ""

def build_polite_escalation_blocks(question: str, channel_id: str, thread_ts: str):
    """친절한 관리자 인계 양식 블록 생성"""
    meta_val = json.dumps({"c": channel_id, "t": thread_ts, "q": question[:80]}, ensure_ascii=False)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "🤖 *AI도 모를 수 있어요.. 해당 내용을 관리자(SV)에게 물어볼까요?*"
            }
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "예 (관리자 질의서 작성)", "emoji": True},
                    "style": "primary",
                    "action_id": "open_escalate_modal",
                    "value": meta_val
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "아니오", "emoji": True},
                    "action_id": "dismiss_escalate",
                    "value": "dismiss"
                }
            ]
        }
    ]

def async_bot_worker(channel_id: str, thread_ts: str, message_ts: str, question: str, is_thread_followup: bool = False):
    try:
        slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="eyes")
    except Exception:
        pass

    # 직접 인계 키워드 감지
    direct_escalate_keywords = ["관리자", "sv", "담당자", "인계", "연결해"]
    is_direct = any(k in question.lower() for k in direct_escalate_keywords)

    # 스레드 이전 대화 맥락 조회
    thread_history, prev_user_queries = get_thread_context(channel_id, thread_ts, message_ts) if is_thread_followup else ("", "")

    # 현재 질문 + 스레드 이전 질문 키워드 결합 검색
    evidence, has_match, best_item = search_memory_knowledge(question, prev_user_queries)

    # 1. 사내 지식에 없거나 직접 인계를 요청한 경우
    if not has_match or is_direct:
        blocks = build_polite_escalation_blocks(question, channel_id, thread_ts)
        try:
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                blocks=blocks,
                text="🤖 AI도 모를 수 있어요.. 해당 내용을 관리자(SV)에게 물어볼까요?"
            )
            slack_client.reactions_remove(channel=channel_id, timestamp=message_ts, name="eyes")
            slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="white_check_mark")
        except Exception as e:
            print(f"❌ 슬랙 전송 실패: {e}")
        return

    # 2. 실무 도메인 구분(해약 vs 청약철회 vs 계약취소) 및 디테일한 상담 코칭 프롬프트
    prompt = f"""당신은 보람상조(대성글로벌네트워크) 상담사를 실시간으로 코칭하는 최고 수준의 AI 어시스턴트 '대성봇'입니다.
제공된 [사내 공식 지식 규정]만을 바탕으로, 상담사가 고객과 통화 중 바로 읽고 응대할 수 있도록 다정하고 명확하며 구체적인 가이드를 작성하세요.

[이전 스레드 대화 맥락]:
{thread_history if thread_history else "없음 (신규 질문)"}

[상담사 현재 질문]:
{question}

[사내 공식 지식 규정 (검색된 자료)]:
{evidence}

[🚨 필수 실무 도메인 구분 원칙 - 절대 혼동 금지!]:
반드시 아래 3가지 개념을 엄격히 구분하여 답변하세요. 절대 서로 섞어서 안내하면 안 됩니다!
1. *계약취소*: 초회 출금/결제 전인 *'임시계약'* 상태에서 취소하는 것입니다. 별도의 구비서류도 필요 없고, 별도의 녹취도 필요 없이 고객 요청에 따라 즉시 취소 처리가 가능합니다.
2. *청약철회 (철회)*: 가입 후 *14일 이내*에 요청하는 철회입니다. 별도의 구비서류는 필요 없으며, 정해진 청약철회 기준(녹취 등)에 따라 처리합니다.
3. *해약 (해지)*: 정식 계약 유지 중(14일 경과 후) 계약을 해지하는 *중도해약*입니다. 반드시 *구비서류*(신분증 등 유형별 필수 서류) 확인 및 해약 프로세스가 필요합니다.

[작성 및 말투 핵심 지침 - 반드시 준수]:
1. 단답형이나 단순 복붙을 절대 금지합니다! 상담사가 이해하기 쉽게 전후 맥락과 조건을 친절한 문장('~합니다', '~입니다')으로 풀어서 설명하세요.
2. 슬랙(Slack) 전용 마크다운을 사용하므로 글씨를 굵게 강조할 때는 별표 두 개(**)가 아닌 별표 한 개(*강조할 단어*)만 사용하세요.
3. 만약 [사내 공식 지식 규정]과 위 원칙을 모두 살펴봐도 상담사의 질문에 답할 근거가 전혀 없다면, 억지로 지어내지 말고 오직 "NO_DATA_FOUND" 라고만 출력하세요.
4. 각 섹션의 역할:
   - 📢 *[핵심 답변]*: 질문에 대한 결론부터 두괄식으로 1~3문장으로 명확히 요약하고, 조건(예: 납부 이력 유무, 계약 상태별 차이 등)에 따라 달라진다면 첫 단락 아래에 불릿(-)으로 한눈에 비교해 주세요.
   - 💡 *[상세 기준]*: 회차별/상황별/유형별 세부 기준, 진행 절차, 필수 구비서류 등을 하위 불릿(-)과 들여쓰기를 활용해 체계적이고 꼼꼼하게 정리하세요.
   - 📌 *[상담 가이드 & 팁]*: 상담사가 전산에서 먼저 확인해야 할 포인트(예: 임시계약 상태인지, 14일 이내인지, 납부 이력이 있는지 등), 오안내 방지를 위한 주의사항, 고객 응대 화법 팁을 2~3가지 풍부하게 작성하세요.

[반드시 준수할 출력 서식]:
📢 *[핵심 답변]*:
(결론 및 조건별 핵심 요약)

💡 *[상세 기준]*:
- *구분/조건 1*: 세부 기준 및 절차
- *구분/조건 2*: 세부 기준 및 절차

📌 *[상담 가이드 & 팁]*:
- (전산 확인 포인트 및 실무 응대 화법 팁 1)
- (예외 상황 대처법 및 주의사항 팁 2)"""

    success, ai_reply = call_gemini_api(prompt)

    # AI가 규정 내에 답이 없다고 판단한 경우 친절한 관리자 확인 버튼 출력
    if success and "NO_DATA_FOUND" in ai_reply:
        blocks = build_polite_escalation_blocks(question, channel_id, thread_ts)
        slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            blocks=blocks,
            text="🤖 AI도 모를 수 있어요.. 해당 내용을 관리자(SV)에게 물어볼까요?"
        )
        try:
            slack_client.reactions_remove(channel=channel_id, timestamp=message_ts, name="eyes")
            slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="white_check_mark")
        except Exception:
            pass
        return

    if success and ai_reply:
        final_text = ai_reply
    else:
        # 모든 API 키가 일시적 한도 초과일 때의 비상 안전망
        final_text = (
            f"📢 *[핵심 답변]*:\n*{best_item['sub']}* 관련 기준 안내드립니다.\n\n"
            f"💡 *[상세 기준]*:\n{format_for_slack(best_item['answer'][:800])}\n\n"
            f"📌 *[상담 가이드 & 팁]*:\n- 현재 AI 요약 요청량이 많아 원문 기준으로 빠르게 안내해 드립니다. 세부 사항은 전산 및 사내 규정을 함께 확인해 주세요."
        )

    try:
        slack_client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=final_text, mrkdwn=True)
    except Exception as e:
        print(f"❌ 슬랙 전송 실패: {e}")

    try:
        slack_client.reactions_remove(channel=channel_id, timestamp=message_ts, name="eyes")
        slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="white_check_mark")
    except Exception:
        pass

@app.get("/")
def health_check():
    return {"status": "ok", "loaded_knowledge_count": len(CACHED_KNOWLEDGE)}

@app.get("/sync")
def manual_sync():
    count = sync_master_knowledge()
    return {"status": "synced", "count": count}

@app.post("/slack/events")
async def handle_slack_events(request: Request, background_tasks: BackgroundTasks):
    # 슬랙 3초 타임아웃 재전송(Retry) 헤더 감지 시 즉시 무시 (중복 응답/중복 버튼 완벽 차단!)
    if request.headers.get("x-slack-retry-num"):
        return {"status": "ignored_retry"}

    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")

    # 1. 인터랙션 (버튼 클릭 및 모달 제출)
    if body_str.startswith("payload="):
        parsed = urllib.parse.parse_qs(body_str)
        raw_payload = parsed.get("payload", ["{}"])[0]
        payload = json.loads(raw_payload)
        p_type = payload.get("type")

        # A. 버튼 클릭 처리
        if p_type == "block_actions":
            action = payload.get("actions", [{}])[0]
            action_id = action.get("action_id")
            trigger_id = payload.get("trigger_id")

            # 1) [예 (관리자 질의서 작성)] 클릭 -> 정통 모달 팝업
            if action_id == "open_escalate_modal":
                action_val = action.get("value", "{}")
                meta_dict = json.loads(action_val)
                orig_q = meta_dict.get("q", "문의 내용 없음")

                modal_view = {
                    "type": "modal",
                    "callback_id": "submit_escalate_modal",
                    "private_metadata": action_val,
                    "title": {"type": "plain_text", "text": "🚨 SV 업무 확인 요청서", "emoji": True},
                    "submit": {"type": "plain_text", "text": "요청서 제출", "emoji": True},
                    "close": {"type": "plain_text", "text": "취소", "emoji": True},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": f"📋 *상담사 원본 질문*\n>{orig_q}"}
                        },
                        {"type": "divider"},
                        {
                            "type": "input",
                            "block_id": "escalate_type_block",
                            "label": {"type": "plain_text", "text": "📌 인계 사유 유형", "emoji": True},
                            "element": {
                                "type": "static_select",
                                "action_id": "escalate_type_select",
                                "placeholder": {"type": "plain_text", "text": "유형을 선택해주세요", "emoji": True},
                                "options": [
                                    {
                                        "text": {"type": "plain_text", "text": "1. 사내 지식 미등록 / 신규 규정 확인", "emoji": True},
                                        "value": "지식_미등록"
                                    },
                                    {
                                        "text": {"type": "plain_text", "text": "2. AI 답변 내용 오류 / 팩트 불일치", "emoji": True},
                                        "value": "답변_오류"
                                    },
                                    {
                                        "text": {"type": "plain_text", "text": "3. 예외 케이스 처리 및 특별 승인 요청", "emoji": True},
                                        "value": "예외_승인"
                                    },
                                    {
                                        "text": {"type": "plain_text", "text": "4. 기타 실무 지침 문의", "emoji": True},
                                        "value": "기타_문의"
                                    }
                                ]
                            }
                        },
                        {
                            "type": "input",
                            "block_id": "reason_block",
                            "label": {"type": "plain_text", "text": "📝 상세 확인 요청 내용", "emoji": True},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "reason_input",
                                "multiline": True,
                                "placeholder": {
                                    "type": "plain_text",
                                    "text": "관리자가 파악해야 할 상세 사유나 고객의 구체적인 상황을 입력해주세요."
                                }
                            }
                        }
                    ]
                }
                try:
                    slack_client.views_open(trigger_id=trigger_id, view=modal_view)
                except Exception as e:
                    print(f"❌ 모달 팝업 오류: {e}")
                return {"status": "ok"}

            # 2) [아니오] 클릭 -> 메시지 취소 처리
            elif action_id == "dismiss_escalate":
                channel_id = payload.get("channel", {}).get("id")
                message_ts = payload.get("message", {}).get("ts")
                try:
                    slack_client.chat_update(
                        channel=channel_id,
                        ts=message_ts,
                        text="취소되었습니다.",
                        blocks=[{
                            "type": "context",
                            "elements": [{"type": "mrkdwn", "text": "ℹ️ *관리자 확인 요청이 취소되었습니다.*"}]
                        }]
                    )
                except Exception:
                    pass
                return {"status": "ok"}

        # B. 모달 제출 처리
        elif p_type == "view_submission":
            user_info = payload.get("user", {})
            user_name = user_info.get("name", "상담사")
            user_id = user_info.get("id", "")
            
            meta = json.loads(payload.get("view", {}).get("private_metadata", "{}"))
            channel_id = meta.get("c")
            thread_ts = meta.get("t")
            orig_q = meta.get("q", "")

            values = payload.get("view", {}).get("state", {}).get("values", {})
            sel_type = values.get("escalate_type_block", {}).get("escalate_type_select", {}).get("selected_option", {}).get("text", {}).get("text", "일반 인계")
            reason = values.get("reason_block", {}).get("reason_input", {}).get("value", "내용 없음")

            notice_blocks = [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "🚨 [관리자(SV) 업무 인계 접수]", "emoji": True}
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*요청 상담사:*\n<@{user_id}> ({user_name})"},
                        {"type": "mrkdwn", "text": f"*인계 유형:*\n`{sel_type}`"}
                    ]
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*상담사 원본 질문:*\n>{orig_q}"}
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*상세 확인 요청 사항:*\n{reason}"}
                },
                {"type": "divider"},
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": "📌 *관리자(SV) 확인 요망*: 내용을 검토하신 후 본 스레드 댓글로 업무 지침을 남겨주세요."}
                    ]
                }
            ]

            try:
                slack_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    blocks=notice_blocks,
                    text=f"🚨 [관리자 확인 요청] {orig_q}"
                )
            except Exception as e:
                print(f"❌ 인계 카드 전송 실패: {e}")

            return {"response_action": "clear"}

    # 2. 슬랙 메시지 이벤트
    try:
        data = json.loads(body_str)
    except Exception:
        return {"status": "ignored"}

    if data.get("type") == "url_verification":
        return {"challenge": data.get("challenge")}

    # 이벤트 ID 기반 2중 중복 방어
    event_id = data.get("event_id")
    if event_id:
        if event_id in PROCESSED_EVENTS:
            return {"status": "already_processed"}
        PROCESSED_EVENTS[event_id] = True
        if len(PROCESSED_EVENTS) > 500:
            PROCESSED_EVENTS.popitem(last=False)

    event = data.get("event", {})
    if event.get("type") == "message" and not event.get("bot_id") and not event.get("subtype"):
        text = event.get("text", "").strip()
        channel_id = event.get("channel")
        message_ts = event.get("ts")
        thread_ts = event.get("thread_ts")

        # 메인 채널 신규 질문
        if text.startswith("[질의-AI]"):
            question = text.replace("[질의-AI]", "").strip()
            background_tasks.add_task(
                async_bot_worker,
                channel_id,
                thread_ts if thread_ts else message_ts,
                message_ts,
                question,
                False
            )
        # 스레드 내 댓글 (후속 질의)
        elif thread_ts and thread_ts != message_ts:
            if len(text) >= 2:
                background_tasks.add_task(
                    async_bot_worker,
                    channel_id,
                    thread_ts,
                    message_ts,
                    text,
                    True
                )

    return {"status": "ok"}

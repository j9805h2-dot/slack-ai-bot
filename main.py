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

app = FastAPI()

# 1. 환경변수 로드 및 API 키 수집
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")

GEMINI_API_KEYS = []
for i in range(1, 6):
    k = os.environ.get(f"GEMINI_API_KEY_{i}")
    if k and k.strip():
        GEMINI_API_KEYS.append(k.strip())

fallback_key = os.environ.get("GEMINI_API_KEY")
if fallback_key and fallback_key.strip():
    GEMINI_API_KEYS.append(fallback_key.strip())

GEMINI_API_KEYS = list(dict.fromkeys(GEMINI_API_KEYS))
print(f"🔑 로드된 Gemini API 키 개수: {len(GEMINI_API_KEYS)}개")

slack_client = WebClient(token=SLACK_BOT_TOKEN)

PROCESSED_EVENTS = OrderedDict()
KEY_INDEX = 0

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
                        kw_list.extend(re.findall(r"[\w가-힣&]+", q.lower()))
                        kw_list.extend(re.findall(r"[\w가-힣&]+", sub.lower()))
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

# 3. 동의어 사전 (상품명 & 특수기호 매핑)
SYNONYM_MAP = {
    "b&케어팩": ["비앤케어팩", "b케어팩", "케어팩", "삼성전자", "삼성스토어", "가전구독", "캐시백"],
    "비앤케어팩": ["b&케어팩", "b케어팩", "케어팩", "삼성전자", "가전구독", "캐시백"],
    "b케어팩": ["b&케어팩", "비앤케어팩", "케어팩", "삼성전자"],
    "케어팩": ["b&케어팩", "비앤케어팩", "삼성전자", "가전구독"],
    "해약": ["해지", "중도해약", "해약서류", "구비서류", "환급금", "해약환급금"],
    "해지": ["해약", "중도해약", "해약서류", "구비서류", "환급금"],
    "철회": ["청약철회", "14일", "철회녹취"],
    "청약철회": ["철회", "14일", "철회녹취"],
    "계약취소": ["임시계약", "취소", "청약취소", "접수취소"],
    "임시계약": ["계약취소", "취소", "결제전"],
    "캐시백": ["캐시백상품", "72개월", "청구할인", "제휴카드", "환급", "만기", "b&케어팩"],
    "제휴카드": ["캐시백", "할인", "청구할인", "롯데카드", "하나카드", "kb카드"],
    "스카이펫": ["펫", "반려동물", "강아지", "고양이", "펫상품", "가입조건"],
    "펫": ["스카이펫", "반려동물", "펫상품"],
    "웨딩": ["결혼", "웨딩상품", "웨딩컨설팅", "예식", "행사"],
    "여행": ["크루즈", "여행상품", "크루즈상품", "관광", "만기환급", "전환"],
    "크루즈": ["여행", "크루즈상품", "여행상품", "선박"],
    "상조": ["장례", "행사", "상조상품", "구좌", "만기", "완납", "행사진행"],
    "행사": ["장례", "상조", "의전", "인력", "용품", "임종", "발인"],
    "장기연체": ["연체", "미납", "7회", "출금", "재출금", "직권해지"],
    "연체": ["미납", "출금", "재출금", "납부"],
    "미납": ["연체", "출금", "재출금", "납부"],
    "서류": ["구비서류", "신분증", "초본", "확인서", "대체서류", "개명", "외국인"],
    "개명": ["개명", "초본", "주민등록초본"],
    "외국인": ["외국인등록증", "실명인증"],
    "대납": ["대리납부", "납부자변경", "타인납부", "직계가족"],
    "대리납부": ["대납", "납부자변경", "직계가족", "동의녹취"],
}

def extract_core_stems(text: str):
    """특수문자 & 유지 및 한글/영문 형태소 정밀 분리"""
    normalized = text.lower().replace("b &", "b&").replace("b and", "b&")
    clean = re.sub(r"[^\w\s가-힣&]", " ", normalized)
    words = clean.split()
    stems = set()

    stop_words = {"알려줘", "뭐야", "뭐가", "어떻게", "언제", "얼마", "확인해줘", "답변", "부탁", "경우", "경우는", "관련", "그럼", "만약", "필요해", "하나요", "할까요", "있나요", "대해", "설명해줘", "설명"}
    josa_pattern = r"(시에는|일때는|할때는|경우에는|경우는|에서는|이라도|시엔|할때|에는|에서|에게|으로|까지|부터|마다|이면|면|의|과|와|을|를|은|는|이|가|도|만|시|해|요|죠|에)$"

    for w in words:
        if w in stop_words:
            continue
        stems.add(w)
        stripped = re.sub(josa_pattern, "", w)
        if len(stripped) >= 2 and stripped not in stop_words:
            stems.add(stripped)
        if "b&" in w:
            stems.add("b&케어팩")
            stems.add("비앤케어팩")

    expanded = set(stems)
    for s in list(stems):
        if s in SYNONYM_MAP:
            expanded.update(SYNONYM_MAP[s])

    return list(expanded)

def get_thread_context(channel_id: str, thread_ts: str, current_ts: str):
    if not thread_ts or thread_ts == current_ts:
        return "", ""
    try:
        res = slack_client.conversations_replies(channel=channel_id, ts=thread_ts, limit=4)
        msgs = res.get("messages", [])
        if len(msgs) <= 1:
            return "", ""
        
        history_lines = []
        user_keywords_source = []
        for m in msgs[:-1][-3:]:
            is_bot = bool(m.get("bot_id"))
            role = "대성봇" if is_bot else "상담사"
            txt = re.sub(r"\[질의-AI\]", "", m.get("text", "")).strip()
            if not is_bot:
                user_keywords_source.append(txt)
            short_txt = txt[:150].replace("\n", " ")
            history_lines.append(f"- {role}: {short_txt}")
            
        return "\n".join(history_lines), " ".join(user_keywords_source)
    except Exception:
        return "", ""

def search_memory_knowledge(current_query: str, prev_user_queries: str = ""):
    current_stems = extract_core_stems(current_query)
    prev_stems = extract_core_stems(prev_user_queries) if prev_user_queries else []
    clean_q = re.sub(r"[^\w가-힣&]", "", current_query.lower())

    scored = []
    for item in CACHED_KNOWLEDGE:
        score = 0
        sub_clean = re.sub(r"[^\w가-힣&]", "", item["sub"].lower())
        q_clean = re.sub(r"[^\w가-힣&]", "", item["question"].lower())
        ans_clean = item["answer"].lower()

        for stem in current_stems:
            if stem in sub_clean: score += 150
            if stem in q_clean: score += 100
            for kw in item["keywords"]:
                if stem == kw: score += 80
                elif stem in kw or kw in stem: score += 35
            if stem in ans_clean: score += 20

        for p_stem in prev_stems:
            if p_stem not in current_stems:
                if p_stem in sub_clean: score += 40
                if p_stem in q_clean: score += 25
                if p_stem in ans_clean: score += 10

        if len(clean_q) >= 2 and (clean_q in sub_clean or sub_clean in clean_q or clean_q in q_clean):
            score += 150

        if score >= 20:
            scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    
    if scored:
        top_items = [s[1] for s in scored[:3]]
        combined_text = "\n\n---\n\n".join([
            f"[{it['sub']} | {it['question']}]\n{it['answer']}"
            for it in top_items
        ])
        return combined_text, True, top_items[0]
    return "", False, None

def format_for_slack(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"^#{1,3}\s*(.+)$", r"*\1*", text, flags=re.MULTILINE)
    return text.strip()

def call_gemini_api_rest(prompt: str):
    global KEY_INDEX
    if not GEMINI_API_KEYS:
        return False, "ERROR: NO_API_KEYS_CONFIGURED"

    models = ["gemini-3.8-flash", "gemini-flash-latest"]
    num_keys = len(GEMINI_API_KEYS)
    last_error_code = "UNKNOWN_ERROR"

    for attempt in range(2):
        for i in range(num_keys):
            current_key = GEMINI_API_KEYS[(KEY_INDEX + i) % num_keys]
            for m in models:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={current_key}"
                headers = {"Content-Type": "application/json"}
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.3,
                        "maxOutputTokens": 1500
                    }
                }
                try:
                    res = requests.post(url, headers=headers, json=payload, timeout=20)
                    status = res.status_code
                    
                    if status == 200:
                        data = res.json()
                        text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        if text:
                            KEY_INDEX = (KEY_INDEX + i + 1) % num_keys
                            return True, format_for_slack(text)
                    elif status == 429:
                        last_error_code = "429_RESOURCE_EXHAUSTED"
                        time.sleep(1.5)
                        continue
                    else:
                        last_error_code = f"HTTP_{status}"
                except Exception as e:
                    last_error_code = f"EXCEPTION_{str(e)[:15]}"
                    time.sleep(1.0)
                    continue
        
        time.sleep(2.5)
                
    return False, last_error_code

def build_polite_escalation_blocks(question: str, channel_id: str, thread_ts: str):
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

    direct_escalate_keywords = ["관리자", "sv", "담당자", "인계", "연결해"]
    is_direct = any(k in question.lower() for k in direct_escalate_keywords)

    thread_history, prev_user_queries = get_thread_context(channel_id, thread_ts, message_ts) if is_thread_followup else ("", "")
    evidence, has_match, best_item = search_memory_knowledge(question, prev_user_queries)

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
        except Exception:
            pass
        return

    # 다정하고 따뜻한 톤앤매너 프롬프트 (웃는 얼굴 및 이모티콘 적극 활용)
    prompt = f"""당신은 항상 밝은 미소로 상담사님들을 든든하게 응원해 드리는 보람상조의 최고 친절한 AI 어시스턴트 '대성봇'입니다! 😊
상담사님이 바쁜 현장에서 힘을 내실 수 있도록 따뜻하고 다정다감한 톤(~해 주시면 되어요! ✨, ~참고해 보세요! 🌸)으로, 적절한 이모티콘을 듬뿍 섞어 상냥하게 안내해 주세요.

[상담사님 질문]: "{question}"

[이전 대화 맥락]:
{thread_history if thread_history else "없음"}

[사내 공식 규정 (검색 결과)]:
{evidence}

[답변 작성 가이드]:
1. 말투 & 톤앤매너:
   - 딱딱한 로봇 말투(~입니다, ~바랍니다)를 지양하고, 옆에서 친절하게 알려주는 동료처럼 다정하고 부드러운 어투(~해 주세요! 😊, ~확인해 주시면 좋습니다! 💡)를 사용하세요.
   - 단락마다 보기 좋고 귀여운 이모티콘(✨, 💡, 🌸, 👏, 📌 등)을 조화롭게 넣어 시각적으로도 따뜻하게 표현해 주세요.
2. 질문 의도에 맞게 살 붙이기:
   - 상품 설명 질문이면 상품의 콘셉트와 고객님께 어필할 수 있는 핵심 결합 혜택을 풍성하게 설명해 주세요.
   - 서류나 조건 질문이면 구비서류 목록과 함께 대체 서류, 필수 주의사항을 꼼꼼하게 챙겨주세요.
3. 슬랙 마크다운 규칙: 볼드 처리는 별표 하나(*강조*)를 사용하세요.

[반드시 준수할 출력 서식]:
📢 *[핵심 답변]*:
(상냥한 도입 인사와 함께 질문에 대한 명확한 핵심 답변 요약 ✨)

💡 *[상세 기준]*:
- *구분/조건*: 상세 내용 및 진행 기준 꼼꼼 정리 🌸

📌 *[상담 가이드 & 꿀팁]*:
- 상담사님이 고객님께 안내하실 때 쓰면 좋은 다정한 멘트 팁 및 체크포인트 😊"""

    success, ai_reply = call_gemini_api_rest(prompt)

    if success and ai_reply:
        final_text = ai_reply
    else:
        final_text = (
            f"📢 *[핵심 답변]*:\n상담사님, {best_item['sub']} 관련 규정 바로 확인해 드려요! 😊✨\n\n"
            f"💡 *[상세 기준]*:\n{best_item['answer']}\n\n"
            f"📌 *[상담 가이드 & 꿀팁]*:\n- 최신 지침 확인하시어 고객님께 친절히 안내해 주세요! 오늘도 파이팅입니다! 🌸"
        )

    try:
        slack_client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=final_text, mrkdwn=True)
    except Exception:
        pass

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
    if request.headers.get("x-slack-retry-num"):
        return {"status": "ignored_retry"}

    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")

    if body_str.startswith("payload="):
        parsed = urllib.parse.parse_qs(body_str)
        raw_payload = parsed.get("payload", ["{}"])[0]
        payload = json.loads(raw_payload)
        p_type = payload.get("type")

        if p_type == "block_actions":
            action = payload.get("actions", [{}])[0]
            action_id = action.get("action_id")
            trigger_id = payload.get("trigger_id")

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
                        {"type": "section", "text": {"type": "mrkdwn", "text": f"📋 *상담사 원본 질문*\n>{orig_q}"}},
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
                                    {"text": {"type": "plain_text", "text": "1. 사내 지식 미등록 / 신규 규정 확인", "emoji": True}, "value": "지식_미등록"},
                                    {"text": {"type": "plain_text", "text": "2. AI 답변 내용 오류 / 팩트 불일치", "emoji": True}, "value": "답변_오류"},
                                    {"text": {"type": "plain_text", "text": "3. 예외 케이스 처리 및 특별 승인 요청", "emoji": True}, "value": "예외_승인"},
                                    {"text": {"type": "plain_text", "text": "4. 기타 실무 지침 문의", "emoji": True}, "value": "기타_문의"}
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
                                "placeholder": {"type": "plain_text", "text": "관리자가 파악해야 할 상세 사유나 고객의 구체적인 상황을 입력해주세요."}
                            }
                        }
                    ]
                }
                try:
                    slack_client.views_open(trigger_id=trigger_id, view=modal_view)
                except Exception:
                    pass
                return {"status": "ok"}

            elif action_id == "dismiss_escalate":
                channel_id = payload.get("channel", {}).get("id")
                message_ts = payload.get("message", {}).get("ts")
                try:
                    slack_client.chat_update(
                        channel=channel_id,
                        ts=message_ts,
                        text="취소되었습니다.",
                        blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": "ℹ️ *관리자 확인 요청이 취소되었습니다.*"}]}]
                    )
                except Exception:
                    pass
                return {"status": "ok"}

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
                {"type": "header", "text": {"type": "plain_text", "text": "🚨 [관리자(SV) 업무 인계 접수]", "emoji": True}},
                {"type": "section", "fields": [{"type": "mrkdwn", "text": f"*요청 상담사:*\n<@{user_id}> ({user_name})"}, {"type": "mrkdwn", "text": f"*인계 유형:*\n`{sel_type}`"}]},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*상담사 원본 질문:*\n>{orig_q}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*상세 확인 요청 사항:*\n{reason}"}},
                {"type": "divider"},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": "📌 *관리자(SV) 확인 요망*: 내용을 검토하신 후 본 스레드 댓글로 업무 지침을 남겨주세요."}]}
            ]

            try:
                slack_client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, blocks=notice_blocks, text=f"🚨 [관리자 확인 요청] {orig_q}")
            except Exception:
                pass

            return {"response_action": "clear"}

    try:
        data = json.loads(body_str)
    except Exception:
        return {"status": "ignored"}

    if data.get("type") == "url_verification":
        return {"challenge": data.get("challenge")}

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

        if text.startswith("[질의-AI]"):
            question = text.replace("[질의-AI]", "").strip()
            background_tasks.add_task(async_bot_worker, channel_id, thread_ts if thread_ts else message_ts, message_ts, question, False)
        elif thread_ts and thread_ts != message_ts:
            if len(text) >= 2:
                background_tasks.add_task(async_bot_worker, channel_id, thread_ts, message_ts, text, True)

    return {"status": "ok"}

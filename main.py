import os
import re
from fastapi import FastAPI, Request, BackgroundTasks
from slack_sdk import WebClient
from google import genai
from google.genai import types

app = FastAPI()

# 1. 환경변수 로드
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
API_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
]
GEMINI_API_KEYS = [k.strip() for k in API_KEYS if k]

slack_client = WebClient(token=SLACK_BOT_TOKEN)

# 2. 로컬 메모리 마스터 지식
DEFAULT_KNOWLEDGE = [
    {
        "category": "구비서류",
        "sub": "개명",
        "question": "개명 시 필요 서류 및 절차",
        "answer": "최근 3개월 이내 발급된 주민등록초본(개명 전·후 내역 포함 필수)과 개명 후 신분증(주민등록증, 운전면허증 등 유효 신분증) 원본 사본이 필요합니다.",
        "keywords": ["개명", "서류", "초본", "신분증"]
    },
    {
        "category": "구비서류",
        "sub": "대리납부",
        "question": "대리납부 구비서류 기준",
        "answer": "초회 납부는 [신분증 2개], 초회 이후 납부는 [신분증 2개 + 3개월 내 가족관계증명서 + 통장사본]이 필수입니다.",
        "keywords": ["대리납부", "대납", "가족관계증명서", "직계"]
    },
    {
        "category": "구비서류",
        "sub": "외국인",
        "question": "외국인 실명인증 서류",
        "answer": "외국인등록증 앞·뒷면 사본(체류기간 명시 필수)이 필요합니다.",
        "keywords": ["외국인", "외국인등록증", "체류기간"]
    },
    {
        "category": "상품규정",
        "sub": "비앤케어팩 캐시백",
        "question": "비앤케어팩 캐시백 입금일 및 세부 일정",
        "answer": "정규 캐시백 입금일은 매월 28일입니다.\n- 2~37: 1회 0원 / 2~11회 월 47,500원 / 12~37회 월 37,500원 (총 145만)\n- 4~48: 1~3회 0원 / 4~47회 월 32,230원 / 48회 31,880원 (총 145만)\n- 4~60: 1~3회 0원 / 4~59회 월 25,440원 / 60회 25,360원 (총 145만)\n- 4~72: 1~3회 0원 / 4~71회 월 21,020원 / 72회 20,640원 (총 145만)",
        "keywords": ["캐시백", "캐쉬백", "비앤케어팩", "입금일", "28일"]
    }
]

def search_memory_knowledge(query: str) -> str:
    clean_q = re.sub(r"[^\w\s가-힣]", " ", query.lower())
    matched = []
    for item in DEFAULT_KNOWLEDGE:
        score = sum(3 for kw in item["keywords"] if kw in clean_q)
        if "개명" in clean_q and "개명" in item["keywords"]: score += 10
        if "대납" in clean_q and "대리납부" in item["keywords"]: score += 10
        if "캐시백" in clean_q and "캐시백" in item["keywords"]: score += 10

        if score > 0:
            matched.append((score, f"[{item['sub']} - {item['question']}]\n{item['answer']}"))

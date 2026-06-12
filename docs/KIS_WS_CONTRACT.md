# KIS WebSocket Real-Time Contract (RTM-6 reference)

도메인: 국내(KRX) 실시간 호가(H0STASP0) / 체결(H0STCNT0) **공개 read-only** 시세만.
주문/체결통보/암호화 private frame은 범위 밖이다.

## Reference revision

- Source: `koreainvestment/open-trading-api` (official KIS OpenAPI sample)
- Verified against tree SHA: `33e0e1e65cd1c8c8b639531483ec0b327087bab1` (`main`)
- Verified files:
  - `examples_llm/auth/auth_ws_token/auth_ws_token.py` (approval key)
  - `examples_user/domestic_stock/domestic_stock_functions_ws.py`
    (`asking_price_krx` = H0STASP0, `ccnl_krx` = H0STCNT0 column order)
- 공식 sample은 변경될 수 있다. 필드 순서/엔벨로프가 바뀌면 위 SHA를 갱신하고
  parser 인덱스를 다시 검증한다. 이 문서는 "검증 시점 계약"의 스냅샷이다.

## 1. Approval key (websocket 전용 인증키)

- Method: `POST`
- Path: `/oauth2/Approval`
- Request JSON body keys: `grant_type` (= `client_credentials`), `appkey`, `secretkey`
  - 주의: REST token(`/oauth2/tokenP`)은 `appsecret`를 쓰지만, Approval은 `secretkey`다.
- Response key: `approval_key` (문자열)
- approval_key는 access_token이 아니다. 주문 권한이 없는 websocket 구독 전용 키다.
- 본 키/`appkey`/`secretkey`는 절대 로그·snapshot·evidence·에러 메시지에 남기지 않는다.

## 2. 구독 / 해지 envelope (client → server, JSON text frame)

```
{
  "header": {
    "approval_key": "<approval_key>",
    "custtype": "P",
    "tr_type": "1",            // "1" = 등록(subscribe), "2" = 해지(unsubscribe)
    "content-type": "utf-8"
  },
  "body": {
    "input": {
      "tr_id": "H0STASP0",     // or "H0STCNT0"
      "tr_key": "005930"        // 단축 종목코드
    }
  }
}
```

- `tr_type`: subscribe = `"1"`, unsubscribe = `"2"`.
- ack 응답(JSON)에서 `body.rt_cd != "0"`이면 구독 실패로 보고 fail-closed.
  (ack 메시지의 raw 본문/메시지는 그대로 싣지 않고 sanitized code/symbol만 남긴다.)

## 3. PINGPONG (server → client, JSON text frame)

- `header.tr_id == "PINGPONG"`인 JSON control frame.
- 응답: 동일 payload로 WebSocket protocol **pong** frame을 보낸다(서버 keep-alive).
- liveness용으로 `MarketHeartbeat(channel="PINGPONG")` 1건을 emit한다.
  (transport-health의 ping_received/pong_sent와는 별도 신호다.)

## 4. 시세 frame envelope (server → client, text frame)

```
<flag>|<tr_id>|<data_count>|<body>
```

- `flag`: `0` = 평문(비암호화), `1` = 암호화. **read-only 공개시세는 `0`만 허용**,
  `1`(암호화)이면 fail-closed로 거부한다(복호화 키/private 경로 미연결).
- `tr_id`: `H0STASP0` 또는 `H0STCNT0`.
- `data_count`: body에 직렬된 레코드 수(N). N개 레코드가 연속 `^` 필드로 이어진다.
  레코드당 필드 수 = 아래 표의 길이. body를 `^`로 split한 뒤 레코드 길이 단위로 끊는다.
- `body`: `^`로 구분된 필드 문자열. 한 레코드의 필드는 아래 인덱스 표를 따른다.
- 필드 수가 (레코드길이 × data_count)와 정확히 일치하지 않으면 malformed로 거부.

## 5. H0STASP0 (호가, best bid/ask) — 레코드 필드 인덱스

검증된 컬럼 순서(앞부분만; 전체 59필드):

| idx | field | 사용 |
|-----|-------|------|
| 0  | MKSC_SHRN_ISCD | 종목코드(symbol) |
| 1  | BSOP_HOUR | 영업시간 HHMMSS (quote 시각, frame에 날짜 없음) |
| 2  | HOUR_CLS_CODE | (미사용) |
| 3  | ASKP1 | 최우선 매도호가 → ask_price |
| 4..12 | ASKP2..ASKP10 | (미사용) |
| 13 | BIDP1 | 최우선 매수호가 → bid_price |
| 14..22 | BIDP2..BIDP10 | (미사용) |
| 23 | ASKP_RSQN1 | 최우선 매도호가 잔량 → ask_quantity |
| 24..32 | ASKP_RSQN2..10 | (미사용) |
| 33 | BIDP_RSQN1 | 최우선 매수호가 잔량 → bid_quantity |
| 34..42 | BIDP_RSQN2..10 | (미사용) |

전체 레코드 길이: 59필드(idx 0..58). 본 parser는 길이 검증만 하고 위 6개 필드만 읽는다.

## 6. H0STCNT0 (체결, trade) — 레코드 필드 인덱스

검증된 컬럼 순서(전체 46필드):

| idx | field | 사용 |
|-----|-------|------|
| 0  | MKSC_SHRN_ISCD | 종목코드(symbol) |
| 1  | STCK_CNTG_HOUR | 체결시간 HHMMSS |
| 2  | STCK_PRPR | 체결가 → price |
| 12 | CNTG_VOL | 체결 거래량 → quantity |
| 13 | ACML_VOL | 누적 거래량 → cumulative_volume |
| 33 | BSOP_DATE | 영업일자 YYYYMMDD (체결 날짜) |

전체 레코드 길이: 46필드(idx 0..45).

## 7. 타임스탬프 정책 (Asia/Seoul)

- trade `trade_at` = `BSOP_DATE`(YYYYMMDD) + `STCK_CNTG_HOUR`(HHMMSS), KST.
- quote `quote_at` = 주입된 `received_at`의 KST 날짜 + `BSOP_HOUR`(HHMMSS), KST.
  - quote frame에는 날짜가 없으므로 수신 시각의 KST 날짜를 사용한다.
  - quote 시각이 received_at보다 비정상적으로 미래면 fail-closed(미래 이벤트 거부).
- naive datetime은 만들지 않는다. clock은 주입된 source clock에서 1회 읽는다.

## 8. Sequence 정책

- quote/trade frame에는 provider-native sequence가 없다.
- parser가 `(tr_id, symbol)`별 **수신순서(receive-order)** sequence를 1부터 부여한다.
- 재접속(새 source 인스턴스)마다 fresh parser로 sequence가 1로 reset된다.
- channel 식별자 = `"<tr_id>|<symbol>"` (예: `H0STCNT0|005930`).
- 이 sequence는 "transport receive-order"이며 provider-native 단조 sequence가 아니다.
  MarketMonitor의 epoch-first-event reset 계약과 정렬된다.

## 9. 건강(health) 신호 분리

- transport-health: connected / subscription_sent / ack / ping_received / pong_sent /
  disconnect / unsubscribe_sent / connection_duration.
- market-data-health: trade/quote received / parsed / applied / malformed / stale /
  sequence-order / last-event-time.
- 두 계열은 서로 다른 typed 스냅샷/evidence로 분리한다(혼합 금지).

## 10. 경계 (RTM-6 범위 밖)

- 주문/체결통보/잔고/계좌, 암호화 private frame, 해외/NXT, market-hours 스케줄링,
  MarketMonitor reconnect 예산의 market-hours 전환(RTM-7), 자동 startup/supervisor.
- 네트워크 source는 broker/ledger/paper_loop/paper execution을 import·호출하지 않는다.
- MarketMonitor가 reconnect/backoff/heartbeat-timeout의 단일 소유자다. source는
  "connect 1회 → subscribe → yield → disconnect"만 하고 내부 reconnect 루프가 없다.

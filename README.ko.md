# noroo-dropbay

[English](README.md) | **한국어**

나와 AI가 함께 쓰는, 직접 호스팅하는 파일 업로드 창구입니다.

나는 브라우저로 올리고, AI 어시스턴트는 MCP로 올립니다. 둘 다 서버의 같은 폴더에 원래 파일명 그대로 저장됩니다.

## 세 가지 업로드 경로

| 경로 | 사용자 | 방식 |
|---|---|---|
| 웹 페이지 | 나 | 비밀번호로 로그인해 파일을 끌어다 놓습니다. 조각 단위로 올라가며 이어 올리기가 됩니다 |
| MCP `save_image` | AI 어시스턴트 | 도구 호출 안에 이미지를 base64로 담아 보냅니다 |
| MCP `request_upload` | AI 어시스턴트 | 일회용 업로드 URL을 받은 뒤, 일반 HTTP `PUT`(예: `curl -T`)으로 파일을 보냅니다 |

`request_upload`가 있는 이유는, 큰 파일을 도구 인자에 base64로 담으면 모델의 출력 한도에 걸리기 때문입니다. 그래서 서버가 URL을 대신 내주고, 파일은 일반 HTTP로 전송됩니다. 어시스턴트가 직접 HTTP 요청을 보낼 수 없다면 그 URL을 사용자에게 건네 브라우저로 열게 할 수 있습니다.

## 세부 사항

- **이어 올리기.** 진행 상태가 디스크에 저장되므로 연결이 끊기거나, 페이지를 새로고침하거나, 서버가 재시작돼도 업로드가 이어집니다. 같은 파일을 다시 고르면 멈춘 지점부터 계속합니다.
- **일회용 URL.** `request_upload` URL은 10분 뒤 만료되고 한 번만 쓸 수 있습니다. 전송이 중간에 실패하면 만료 전까지는 같은 URL을 다시 쓸 수 있습니다.
- **원래 파일명 유지.** 완료된 파일은 `data/blobs` 아래에 그대로 저장되므로 서버에서 바로 열어볼 수 있습니다.
- **데이터베이스 없음.** 파일과 `meta.json` 색인뿐입니다.

## 접근 제어

`/healthz`를 제외한 모든 것이 잠겨 있습니다.

- **웹:** 비밀번호(`UPLOAD_PASSWORD`). 로그인하면 30일간 유지되는 서명 쿠키가 설정됩니다.
- **MCP와 API:** 토큰(`UPLOAD_TOKEN`). 커넥터 UI마다 입력할 수 있는 항목이 달라서, 토큰을 다음 형태 중 어느 것으로든 받습니다.
  - `Authorization: Bearer <토큰>`
  - `X-API-Key`, `Api-Key`, `X-Auth-Token`, `X-Access-Token`, `X-Token`, `X-Upload-Token` 중 하나
  - 헤더를 전혀 보낼 수 없는 클라이언트를 위해 URL에 `/mcp/<토큰>` 형태로
- MCP 핸드셰이크(`initialize`, `tools/list`)는 토큰 없이 열려 있습니다. 인증 없는 401 응답을 클라이언트가 "이 서버는 OAuth를 쓴다"로 해석하기 때문입니다. 이 경로로는 도구 이름과 설명만 보이고, 모든 `tools/call`에는 토큰이 필요합니다.

## 실행

```
UPLOAD_TOKEN=<길고-무작위인-문자열> UPLOAD_PASSWORD=<비밀번호> docker compose up --build
```

compose 파일은 `127.0.0.1:9002`에 바인딩하고 `dbnet`이라는 외부 Docker 네트워크에 참여합니다. [noroo-deploy](https://github.com/tinynoroo/noroo-deploy)로 관리하는 서버용으로 작성했기 때문입니다. 단독으로 실행하려면 네트워크를 먼저 만들거나(`docker network create dbnet`) `networks` 줄을 지우세요.

HTTPS 리버스 프록시 뒤에 두세요. 서버가 발급하는 업로드 URL이 올바르게 나오도록 `X-Forwarded-Proto`와 `X-Forwarded-Host`를 신뢰합니다.

그다음 MCP 클라이언트에 `https://<내-호스트>/mcp`를 등록합니다.

| 변수 | 의미 |
|---|---|
| `UPLOAD_TOKEN` | MCP와 API 접근용 토큰 |
| `UPLOAD_PASSWORD` | 웹 페이지 비밀번호. 비어 있으면 웹 로그인이 꺼집니다 |
| `DATA_DIR` | 파일 저장 위치 (기본값 `/app/data`) |

## 스택

Python 3.12와 Flask로 된 파일 하나(`app/server.py`)와 정적 HTML 페이지 두 개.

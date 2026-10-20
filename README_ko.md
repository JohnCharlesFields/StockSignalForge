# StockSignalForge

**easymoneysniper**

[English](README.md) | [中文](README_zh.md) | [日本語](README_ja.md) | **한국어** | [العربية](README_ar.md)

[![CI](https://github.com/JohnCharlesFields/StockSignalForge/actions/workflows/test.yml/badge.svg)](https://github.com/JohnCharlesFields/StockSignalForge/actions/workflows/test.yml)

[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 기반 AI 주식 리서치 작업대입니다. 시세, 뉴스, 눌림목 후보 선별, 개별 종목 분석, 보유 종목 복기와 전략 백테스트를 한곳에서 다룹니다.

> **주요 대상은 미국 주식과 미국 상장 주식 옵션입니다. 중국 본토 A주와 홍콩 주식은 아직 연동 작업 중이며 완전 지원하지 않습니다.** 상위 프레임워크의 어댑터가 있어도 이 프로젝트의 전체 데이터·일정·신호·검증 흐름이 지원된다는 뜻은 아닙니다.

## 시장 지원 범위

| 시장 | 현재 범위 |
| --- | --- |
| 미국 주식 | 일별 후보, 차트, 기업 정보, 뉴스, 관심 목록과 전략 연구가 중심 |
| 미국 주식 옵션 | Call/Put/관망 방향 연구, 이용 가능한 IV/Greeks, 만기60–90일 Call 계약과 청산 시나리오 수동 계산. 데이터 권한·품질에 따름 |
| 중국 A주 | 연동 작업 중. 전체 흐름 검증 미완료 |
| 홍콩 주식 | 연동 작업 중. 전체 흐름 검증 미완료 |

연구 보조 도구이며 수익이나 매수·매도 판단을 보장하지 않습니다. 브로커에 연결하거나 실거래 주문을 자동 제출하지 않습니다. SOXL 실행 실험은 시뮬레이션입니다.

## 주요 기능

- 눌림목 후보 순위, 지속 모니터링 및 예측과 이후 결과 대조.
- 개별 종목 캔들, 추세 구조, 인과적 연구 마커, 기업·업종·공급망과 위험 가격.
- 옵션 방향·변동성 연구, 확보한 호가에 기반한 계약·청산 시나리오 비교.
- 중요도별 뉴스와 실제 자막·문고에 기반한 영상 제작자 의견.
- 관심 목록, 보유 종목 검토, 3단계 신호와 이벤트/GEX 품질 표시.
- 팩터 연구, 백테스트, 버전별 전향 검증, 일일 배치 및 캐시 신선도 확인.

IV, OI, 거래량, 자막, 실적 발표일 또는 과거 구성 종목이 없으면 미확인으로 남깁니다. 0이나 가짜 근거로 채우지 않습니다.

## 화면

![일봉 실험 연구 마커](assets/screenshots/desktop/13-v-swing-chart.png)

[전체 화면 모음](SCREENSHOTS.md). 이미지는 과거 연구 스냅샷이며 수익 검증이 아닙니다. 다국어 문서는 앱의 모든 화면이 번역되었다는 뜻이 아닙니다.

## Docker 시작

Docker Engine/Compose 또는 Linux 컨테이너 모드의 Docker Desktop을 사용합니다. 저장소 루트에서 실행하며 기존 비공개 설정 파일을 덮어쓰지 않습니다.

```powershell
if (!(Test-Path agent/.env)) { Copy-Item agent/.env.example agent/.env }
# Configure agent/.env locally before starting the application.
docker compose up -d --build vibe-trading
```

시작 전에 로컬 `agent/.env`를 편집하세요. [작업대](http://127.0.0.1:19090)와 [상태 확인](http://127.0.0.1:19090/health)을 열 수 있습니다.

```sh
docker compose logs --tail 100 vibe-trading
docker compose stop vibe-trading
```

기본 포트는 localhost에만 바인딩됩니다. 원격 접근에는 인증과 명시적인 신뢰 Origin이 필요합니다.

### 기존 설치 업데이트

스캔·보정·정보 보강 등 실행 중인 작업이 끝날 때까지 기다린 뒤 SQLite 온라인 백업 API로 일관된 백업을 만들고 명명 볼륨을 보존하세요.

```sh
docker compose build vibe-trading
docker compose up -d --no-deps vibe-trading
```

`docker compose down -v`를 실행하지 마세요. 소스 clone만으로 과거 DB, 캐시, 비공개 설정 또는 Cookies가 이전되지 않습니다.

## 데이터와 비공개 설정

| 설정 | 로컬 변수·자료 |
| --- | --- |
| AI 모델 | `agent/.env`의 `LANGCHAIN_PROVIDER`, `LANGCHAIN_MODEL_NAME` 및 공급자 인증 정보 |
| GilData MCP | `GILDATA_MCP_URL`과 `GILDATA_MCP_TOKEN`을 별도 설정. [연동 안내](GILDATA_MCP.md)(중국어) |
| 기타 시세 공급자 | 본인의 API 키와 시장별 사용 권한 |
| API 인증 | `API_AUTH_KEY` |
| 외부 MCP | `config/agent.example.json`, `config/mcp-client.example.json` |

GilData는 선택적 근거 데이터원이며 공동 브랜드나 수익 보증이 아닙니다. 주식 데이터 이용권이 OPRA 옵션 권한을 포함하지 않을 수 있습니다. 시세 날짜와 대체 출처를 확인하세요.

키, 인증 URL, Cookies, DB, 공급자 원시 이력 및 비공개 학습 라벨은 배포하지 않습니다. 백그라운드 갱신·학습은 명시적인 설정이 필요하며 새 모델을 자동 활성화하지 않습니다.

## 확률과 백테스트의 한계

연구 점수, 주식 보정 확률, 거래 순이익 비율, SPY 대비 alpha와 옵션 이익 확률은 서로 다른 지표입니다. 방향이 맞아도 프리미엄, 스프레드, 시간가치와 IV 변화 때문에 옵션은 손실을 낼 수 있습니다. 시나리오 표는 확률 가중 기대값이 아닙니다.

연구 마커는 실제 체결이나 수익의 증거가 아닙니다. 일봉 확정 후 이미 관찰한 종가에 체결되었다고 가정하지 않습니다. 주식 분할, 실제 정보 가용 시각, 과거 구성 종목과 이벤트 변경 이력에는 근거가 필요합니다.

엄격한 5/8거래일 보정 재구축은 시점을 확인할 수 있는 역사 표본이 부족해 새 곡선을 활성화하지 않았습니다. 기존 숫자는 미검증 연구 참고치입니다. 고정 규칙 재검증에서도 안정적인 양의 기대값은 확인되지 않았으며 이미 본 이력을 미사용 테스트로 간주하지 않습니다. 소프트웨어 테스트 통과가 모든 미래정보 누출·과적합의 부재를 증명하지는 않습니다.

## 업데이트·검증·개발

10월20일 브랜드, 분할 회계, 과거 정보 가용 시점, 이벤트 이력, 보정 신뢰도 및 CI 오류를 수정했습니다. 공개된 `162054e`의 GitHub CI는3321개 테스트·45개 하위 테스트 통과, 기존2개 건너뜀입니다. 현재 상태는 위 CI 배지를 확인하고 투자 성과와 구분하세요.

[변경 내역](CHANGELOG.md)(영어) · [실측 테스트 기록](PUBLIC_RELEASE.md)(중국어) · [수정 상세](wiki/RELEASE_20261020_ZH.md)(중국어) · [Call 방법론](wiki/CALL_PLAN_RESEARCH_ZH.md)(중국어)

Python 3.11+와 Node.js 20+를 사용합니다. 개발 API는 `VITE_API_URL`로 지정합니다.

```sh
python -m pip install -e ".[dev]"
pytest --ignore=agent/tests/e2e_backtest --ignore=agent/tests/test_e2e_harness_v2.py --tb=short -q
cd frontend
npm ci
npm run build
```

실제 모델 E2E 테스트에는 별도 활성화와 인증이 필요합니다. 격리 테스트에 운영 볼륨을 연결하지 마세요.

## 출처와 라이선스

[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 프레임워크를 사용합니다. [한국어 원본](README_ko.upstream.md), [영문 원본](README.upstream.md), [중문 원본](README_zh.upstream.md)을 보존합니다.

[MIT License](LICENSE), [NOTICE](NOTICE). 소프트웨어 라이선스는 제3자 데이터 접근 또는 재배포 권한을 부여하지 않습니다.

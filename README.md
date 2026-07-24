# hermes-galileo

[Hermes Agent](https://github.com/NousResearch/hermes-agent) の実行を[Galileo](https://docs.galileo.ai/sdk-api/overview)へ直接送るObservability pluginです。

Hermesの`hermes.observer.v1` hookをOpenTelemetry GenAI spanに変換し、Galileo公式Python SDKの`GalileoSpanProcessor`でexportします。
[hermes-otel](https://briancaffey.github.io/hermes-otel/)と同じplugin設定方式を使いますが、任意のOTLP backendではなくGalileoに特化しています。

```text
invoke_agent Hermes Agent                 one trace per user turn
├── chat {model}                          provider API attempt
├── execute_tool {tool_name}
│   └── approval_request
└── invoke_agent {subagent_role}
    └── invoke_agent Hermes Subagent
```

主な性質は次のとおりです。

- Hermes の結果を変えない fail-open observer
- turn、API request、tool、approval、subagent の並行相関
- Galileo 公式の認証、project、log stream、OTLP exporter
- Agent 起動を network 待ちさせない遅延 SDK 接続と 2,048 span の起動 buffer
- prompt、response、tool payload を既定で取得しない privacy 設定
- sensitive mapping key、JWT、既知cloud keyなどのredaction、HMAC user pseudonym、payload 上限
- private `TracerProvider`、trace 単位の head sampling、TTL cleanup

## 対応範囲

2026-07-24時点で、Hermes Agent `hermes.observer.v1`と`galileo[otel] >=2.5.1,<3`を対象にしています。
Pythonは3.10以上3.15未満です。
OpenTelemetry GenAI Semantic ConventionsはDevelopment statusなので、dependency更新時はcontract testの再確認が必要です。

## Install

Hermes の現在の third-party plugin は opt-in です。

```bash
hermes plugins install nyasukun/hermes-galileo --enable
```

installerは`plugin.yaml`の必須環境変数を対話的に設定しますが、Python dependencyは自動では入りません。
通常のmanaged installでは次を実行します。

```bash
uv pip install \
  --python ~/.hermes/hermes-agent/venv/bin/python \
  -e ~/.hermes/plugins/hermes_galileo
```

Hermesを別のvenvで実行している場合は、`--python`にそのinterpreterを指定してください。
`uv`がなければ、その環境の`python -m pip install -e ~/.hermes/plugins/hermes_galileo`でも構いません。
installed packageはHermes entry pointから`register`を持つmoduleとして読み込まれます。
test suiteは実際のinstalled distribution metadataからこのentry pointをloadします。

設定後、実行中の`hermes gateway`は再起動します。
CLIは次回起動時にpluginを読み込みます。

## Configuration

最低限、`~/.hermes/.env` に Galileo 公式の三つの変数を設定します。

```dotenv
GALILEO_API_KEY=...
GALILEO_PROJECT=hermes-agent
GALILEO_LOG_STREAM=production

# Self-hosted / custom deployment では二つとも指定
# GALILEO_CONSOLE_URL=https://console.example.com
# GALILEO_API_URL=https://api.example.com
```

Hermes の installer から設定しなかった場合は、直接追記してください。
`GALILEO_CONSOLE_URL`を設定する場合は、process globalなSDKからAPI endpointを暗黙に継承しないよう`GALILEO_API_URL`も必ず指定してください。

### Privacy

実contentは既定で送信しません。
Galileoの必須input/output fieldには`[content capture disabled]`を設定し、token、model、duration、statusなどの運用metadataは送信します。

```dotenv
# 明示的に有効化した場合も、既知 secret は常に redaction されます
HERMES_GALILEO_CAPTURE_CONTENT=false

# root input に会話履歴全体を使うための独立した opt-in
HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY=false

# production 推奨
# 未設定時は Galileo API key を HMAC key に使います
HERMES_GALILEO_PSEUDONYM_SECRET=...
```

hidden chain-of-thoughtは収集しません。
contentと会話履歴の取得を有効にした場合も、known reasoning field、Anthropic thinking blockとsignature、Gemini `thought_signature`は`[REDACTED REASONING]`に置換し、自由text途中のbase64 data URIも省略します。
content取得だけを有効にしても過去の会話は送りません。
`HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY=true`の場合だけ、provider requestの蓄積済みmessage履歴を送ります。

### Runtime

| 変数 | 既定値 | 説明 |
| --- | ---: | --- |
| `HERMES_GALILEO_ENABLED` | `true` | plugin 全体の有効化 |
| `HERMES_GALILEO_HASH_USER_IDS` | `true` | raw user ID を送らない |
| `HERMES_GALILEO_MAX_CONTENT_CHARS` | `12000` | content 一件の文字数上限 |
| `HERMES_GALILEO_MAX_COLLECTION_ITEMS` | `100` | collection の要素上限 |
| `HERMES_GALILEO_SAMPLE_RATE` | `1.0` | root trace の head sampling 率 |
| `HERMES_GALILEO_TURN_TTL_SECONDS` | `900` | 未終了 turn state の TTL |
| `HERMES_GALILEO_ASYNC_FLUSH_ON_TURN_END` | `true` | turn 終了 flush を background 化 |
| `HERMES_GALILEO_FLUSH_TIMEOUT_MILLIS` | `10000` | force flush、OTLP export、各cleanup待ちのtimeout |
| `HERMES_GALILEO_ENVIRONMENT` | `development` | deployment resource 属性 |
| `HERMES_GALILEO_SERVICE_NAME` | `hermes-agent` | OTel service name |

全設定と許容範囲は [運用設計](docs/OPERATIONS.md) にあります。

## Trace model

一回のHermes user turnを一つのtraceとし、rootは`invoke_agent Hermes Agent`です。
会話をまたぐgroupingには同じ`gen_ai.conversation.id`を設定します。

LLM spanにはaggregate prompt token、output token、cache read/write、reasoning tokenを記録し、明示されたzero値も保持します。
課金の二重計上を避けるため、tokenとprovider costは実際のAPI spanだけに設定します。
同じ論理API requestのretryは同じ`hermes.api.request_id`を維持し、試行spanへ1始まりの`hermes.api.attempt`を設定します。
rootのAPI call数はretry回数ではなく、一意な論理request数です。

現行版はGalileo native sessionを作りません。
native session provisioning、external ID、reset/reopen lifecycleは将来要件です。

## Reliability

公式SDKはprocessor作成時にhealth check、API-key login、current-user validationを同期実行します。
本pluginはその処理をdaemon threadへ分離し、接続前とstartup replay中に完了したspanをmemory上で最大2,048件保持します。
一時的な接続失敗は1秒、5秒、30秒、60秒を基準にjitter付きで再試行します。
非retryableなstatus付きerror、`ImportError`、`TypeError`、`ValueError`、process内のGalileo SDK設定競合では再試行を止め、`failed`として公開します。
startup replayが完了した後だけ`ready`になり、状態は `health_snapshot()` で確認できます。

```python
from hermes_galileo import health_snapshot

print(health_snapshot())
# {
#   "enabled": True,
#   "exporter_ready": True,
#   "exporter_state": "ready",
#   "buffered_spans": 0,
#   "dropped_spans": 0,
#   "connection_attempts": 1,
#   "last_connection_error_retryable": None,
#   "retry_stopped_reason": "",
#   "connector_cleanup_deferred": False,
#   ...
# }
```

このbufferは永続spoolではありません。
process crash、SDK queue overflow、HTTP 200のOTLP partial successに対する完全な配送保証が必要なら、OpenTelemetry CollectorのWAL、queue、tail samplingを検討してください。
SDKが利用する現在のOTel HTTP exporterは429と`Retry-After`をOTLP仕様どおり処理しないため、厳密なretry SLOにもCollectorが必要です。

## Development and test

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

test suite は次を含みます。

- 設定、privacy、redaction の unit test
- real OpenTelemetry SDK を使う hierarchy、error、concurrency test
- SDK の遅延接続、startup replay後のready、jitter付き一時error retry、恒久error停止、bounded shutdown test
- local HTTP stub に対し、公式 Galileo SDK の
  health/login/current_user/OTLP protobuf を通す wire-level E2E
- 401、429、503、408、connection reset、read timeout、partial success、
  large payloadを再現する公式SDK wire failure matrix

wire-level E2E は外部 credential を使いません。
実 Galileo 画面/API から ingestion と privacy canary を read-back する live
E2E は、専用 project と API key が必要な運用受入項目です。

## Documents

- [技術調査](docs/RESEARCH.md)
- [要件定義](docs/REQUIREMENTS.md)
- [設計](docs/DESIGN.md)
- [運用、SLO、E2E手順](docs/OPERATIONS.md)

## Known boundaries

- Galileo native session、永続 spool、tail sampling は未実装です。
- Galileo SDK 内部 queue の全 metric と OTLP partial rejection 本文は取得できません。
- ready 後の OTel HTTP exporter は 429 を再試行せず、`Retry-After` も利用しません。
- sampling は head sampling です。
  error/latency/cost による tail sampling は Collector の責務です。
- `force_flush()` は timeout 内でreadyを待ってSDK queueの処理を要求するbest-effort APIであり、Galileoでの永続化を証明するackではありません。
- flush timeoutはOTLP export HTTP timeoutとready待ちに使いますが、bootstrap時のhealth、login、current-user request timeoutは制御しません。
- 現行SDKのbootstrap requestは既定で最大60秒待ち得ます。
  connector daemonがruntime shutdown後もconstructor、startup replay、またはcleanupを続ける間は、保持したruntime参照の`health_snapshot()`で`connector_cleanup_deferred=true`になります。
- background flusherとdeferred processorは別々にflush timeoutを使うため、process全体のshutdown wall-clockを一つのtimeoutには収めません。

License: [Apache-2.0](LICENSE)

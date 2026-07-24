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
- Hermes sessionをHMAC external IDで対応づけるGalileo native Session
- 一つのnative Sessionに複数のHermes turn traceを格納する会話model
- Agent 起動を network 待ちさせない遅延 SDK 接続と 2,048 span の起動 buffer
- Session APIをhook経路から外す有界queue、single-flight、fail-open replay
- prompt、response、tool payload を既定で取得しない privacy 設定
- sensitive mapping key、JWT、既知cloud keyなどのredaction、HMAC user/session pseudonym、payload 上限
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
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
uv pip install \
  --python ~/.hermes/hermes-agent/venv/bin/python \
  -e "$hermes_home/plugins/hermes_galileo"
```

Hermesを別のvenvで実行している場合は、`--python`にそのinterpreterを指定してください。
`uv`がなければ、その環境の`python -m pip install -e "$hermes_home/plugins/hermes_galileo"`でも構いません。
installed packageはHermes entry pointから`register`を持つmoduleとして読み込まれます。
test suiteは実際のinstalled distribution metadataからこのentry pointをloadします。

設定後、実行中の`hermes gateway`は再起動します。
CLIは次回起動時にpluginを読み込みます。

## Configuration

最低限、active profileの`$HERMES_HOME/.env`にGalileo公式の三つの変数を設定します。
`HERMES_HOME`未指定時は`~/.hermes/.env`です。

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
`GALILEO_API_KEY`、送信先、endpoint、`HERMES_GALILEO_PSEUDONYM_SECRET`は`config.yaml`へ書かないでください。

### Behavior configuration

telemetryの取得範囲、sampling、上限、timeout、resource属性、native Session動作は、hermes-otelと同じ配置の`config.yaml`で設定できます。

```bash
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
cp "$hermes_home/plugins/hermes_galileo/config.yaml.example" \
  "$hermes_home/plugins/hermes_galileo/config.yaml"
```

設定値は、`HERMES_GALILEO_*`環境変数、`config.yaml`、組み込み既定値の順で優先します。
空の環境変数は未指定として扱い、同じfieldのYAML値を消しません。
`config.yaml`が存在しなければ、環境変数と組み込み既定値だけで動作します。
YAMLのrootがmappingでない場合、構文が壊れている場合、未知fieldがある場合、secretまたはGalileo routing fieldがある場合は、その起動でobservabilityだけを無効化します。
Hermes本体の起動と応答は継続します。

v1のGalileo routingは、runtime初期化時のactive Hermes profileへbindingします。
同じprocessで別profileをmultiplexしたeventは誤配送を避けるため観測せず、healthの`profile_scope_mismatches`へ記録します。
複数profileを観測する場合はprofileごとにHermes processを分けてください。

### Privacy

実contentは既定で送信しません。
Galileoの必須input/output fieldには`[content capture disabled]`を設定し、token、model、duration、statusなどの運用metadataは送信します。

```dotenv
# 明示的に有効化した場合も、既知 secret は常に redaction されます
HERMES_GALILEO_CAPTURE_CONTENT=false

# root input に会話履歴全体を使うための独立した opt-in
HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY=false

# production 推奨
# user ID と Hermes session ID の仮名化に使います
# 未設定時は Galileo API key を HMAC key に使います
HERMES_GALILEO_PSEUDONYM_SECRET=...
```

hidden chain-of-thoughtは収集しません。
contentと会話履歴の取得を有効にした場合も、known reasoning field、Anthropic thinking blockとsignature、Gemini `thought_signature`は`[REDACTED REASONING]`に置換し、自由text途中のbase64 data URIも省略します。
content取得だけを有効にしても過去の会話は送りません。
`HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY=true`の場合だけ、provider requestの蓄積済みmessage履歴を送ります。

GalileoのConversation Qualityはnative Session単位でtraceのinputとoutputを評価します。
通常profileはcontent取得を無効のままにします。
Conversation Qualityを使う会話評価profileだけは、privacy審査済みdataを対象に`HERMES_GALILEO_CAPTURE_CONTENT=true`を明示し、Galileo log stream側でも同metricを有効化してください。
Session内の全turnを評価する場合は`HERMES_GALILEO_SAMPLE_RATE=1.0`を使います。

### Runtime

| YAML field | 環境変数 | 既定値 | 説明 |
| --- | --- | ---: | --- |
| `enabled` | `HERMES_GALILEO_ENABLED` | `true` | plugin 全体の有効化 |
| `hash_user_ids` | `HERMES_GALILEO_HASH_USER_IDS` | `true` | raw user ID を送らない |
| `max_content_chars` | `HERMES_GALILEO_MAX_CONTENT_CHARS` | `12000` | content 一件の文字数上限 |
| `max_collection_items` | `HERMES_GALILEO_MAX_COLLECTION_ITEMS` | `100` | collection の要素上限 |
| `sample_rate` | `HERMES_GALILEO_SAMPLE_RATE` | `1.0` | root trace の head sampling 率 |
| `turn_ttl_seconds` | `HERMES_GALILEO_TURN_TTL_SECONDS` | `900` | 未終了 turn state の TTL |
| `async_flush_on_turn_end` | `HERMES_GALILEO_ASYNC_FLUSH_ON_TURN_END` | `true` | turn 終了 flush を background 化 |
| `flush_timeout_millis` | `HERMES_GALILEO_FLUSH_TIMEOUT_MILLIS` | `10000` | force flush、OTLP export、各cleanup待ちのtimeout |
| `native_sessions_enabled` | `HERMES_GALILEO_NATIVE_SESSIONS_ENABLED` | `true` | native Session の作成または再利用 |
| `native_session_timeout_millis` | `HERMES_GALILEO_NATIVE_SESSION_TIMEOUT_MILLIS` | `5000` | local Session解決期限とpending span保持時間 |
| `environment` | `HERMES_GALILEO_ENVIRONMENT` | `development` | deployment resource 属性 |
| `service_name` | `HERMES_GALILEO_SERVICE_NAME` | `hermes-agent` | OTel service name |

content、conversation history、debugを含む全fieldと許容範囲は[運用設計](docs/OPERATIONS.md)にあります。

## Trace model

一回のHermes user turnを一つのtraceとし、rootは`invoke_agent Hermes Agent`です。
同じHermes sessionに属する複数turnは、同じGalileo native Sessionへ入ります。
公式`GalileoLogger.start_session(external_id=...)`でSessionを作成または再利用し、返却UUIDを`galileo.session.id`として関連spanへ設定します。
Galileo external ID、`gen_ai.conversation.id`、`hermes.session.id`には同じ`hermes:`接頭辞付きHMAC仮名値を使い、raw Hermes session IDはexportしません。
subagentのchild sessionはtop-level parentの会話HMACとnative Sessionを共有し、child固有HMACはsubagent相関属性だけに保持します。

LLM spanにはaggregate prompt token、output token、cache read/write、reasoning tokenを記録し、明示されたzero値も保持します。
課金の二重計上を避けるため、tokenとprovider costは実際のAPI spanだけに設定します。
同じ論理API requestのretryは同じ`hermes.api.request_id`を維持し、試行spanへ1始まりの`hermes.api.attempt`を設定します。
rootのAPI call数はretry回数ではなく、一意な論理request数です。

`on_session_end`はturnだけを閉じ、native Session対応を維持します。
finalizeまたはresetはlocal mappingを解放しますが、実行中またはqueue待ちのsubagent delegationがあればcanonicalな`subagent_stop`までmappingとaliasを維持します。
Session解決中ならgeneration単位の成功、timeout、cancel callbackで対象spanだけを解放してからmappingを破棄します。
同じexternal IDで再開した場合は公式SDKの既存Session再利用を使います。
Session APIがtimeoutまたは失敗した場合もHermesを止めず、Session IDなしでtraceをfail-open exportします。
Session制御面は二つのdaemon worker、512件のmapping、512件の要求queue、4096件のpending-ended-span bufferで有界化します。
このtimeoutはlocal mappingとpending spanの期限であり、公式SDK内で実行中の`start_session()`をcancelまたはHTTP timeout設定するものではありません。
両workerがSDK call内で停止した場合はqueue上の後続要求も期限切れになり得るため、`native_session_worker_calls_inflight`、queue depth、timeoutを監視します。
`HERMES_GALILEO_NATIVE_SESSIONS_ENABLED=false`は緊急degradation用であり、native Sessionを必須とするv1受入では使いません。

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
Direct profileの保証は、process内のlocal acceptanceとbest effort flushまでです。
SDKが利用する現在のOTel HTTP exporterはconnection error、408、5xxを再試行しますが、429と`Retry-After`を扱わず、HTTP 200のOTLP partial success本文を解析しません。
これはDirect profileの既知dependency contractであり、adapterの未充足要件ではありません。
429 retry、partial success計測、process crash後の復元、tail samplingが必要な環境は、WALと送信queueを持つCollector profileを選択してください。

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
- local HTTP stubに対し、公式Galileo SDKのhealth、login、current_user、OTLP protobufを通すwire-level E2E
- 401、429、503、408、connection reset、read timeout、partial success、large payloadを再現する公式SDK wire failure matrix
- official Session APIの作成または再利用、HMAC external ID、`galileo.session.id`、複数turn、timeout時fail-openのcontract test

wire-level E2E は外部 credential を使いません。
実 Galileo 画面/API からnative Session、複数turn、ingestion、privacy canary、Conversation Qualityをread-backするlive E2Eは、専用project、log stream、API keyが必要な運用受入項目です。
Conversation QualityはLLM-as-a-judge metricであるため、`GALILEO_API_KEY`だけでは計算できません。
Galileo ConsoleのIntegrationsでjudge用LLM integrationを有効にし、専用log streamのmetric samplingを100%にしてください。
専用projectは事前に作成し、live E2Eは公式SDKで専用log streamを作成または再利用してConversation Qualityを送信前に有効化します。
既存のConversation Quality設定は変更せず、metric未設定のstreamだけを構成します。
別metricだけが設定されたstreamは上書きせず失敗するため、productionや共有log streamを指定しないでください。

GitHub Actionsは、pull requestごとにPython 3.10、3.12、3.14でlocal contractを検証します。
別のsecret不要jobでbaselineの実Hermes sourceを取得し、PluginManager registrationと全observer hookの互換性も検証します。
日次WorkflowはHermes mainのcommit SHA、PyPI project metadataが示すGalileo current release、およびpipが解決したGalileoの全依存closureを確認し、更新時だけ検出したexact closureで全testを実行します。
同一repositoryの信頼済みpull request、`main`へのpush、`test.yml`の手動run、更新を検出したdaily run、`force_test=true`のdependency watchでは`GALILEO_API_KEY`を必須にしてlive E2Eを実行し、secret欠落時はfail-closedにします。
更新がないdaily runはversion比較だけで完了し、live E2Eとsecret確認を行いません。
fork pull requestとDependabotへsecretは渡さず、live E2Eだけをsafe skipします。
これらはlive受入済みとは扱わず、merge前に同じcommitを同一repositoryのtrusted branchで再検証します。

## Documents

- [技術調査](docs/RESEARCH.md)
- [要件定義](docs/REQUIREMENTS.md)
- [設計](docs/DESIGN.md)
- [運用、SLO、E2E手順](docs/OPERATIONS.md)

## Responsibility boundaries

- Direct adapterは、Session対応、semantic mapping、export前privacy、process内の有界状態、bootstrap接続、local healthを所有します。
- Galileo SDK内部queueの全metric、OTLP partial rejection、最終送信成功はDirect adapterから取得しません。
- ready後のOTel HTTP exporterは429を再試行せず、`Retry-After`も利用しません。
- Direct profileのsamplingはhead samplingです。
- data-plane retry、partial success解析、永続WAL、crash recovery、tail samplingはCollector profileの責務です。
- native Sessionの保存、read-back、external IDのprocess間一意性、Conversation Qualityの計算はGalileoと運用環境に依存します。
- native Sessionのlocal timeoutは実行中の公式SDK callをcancelせず、worker cleanupがtimeout後もdaemonで続く場合があります。
- `force_flush()` は timeout 内でreadyを待ってSDK queueの処理を要求するbest-effort APIであり、Galileoでの永続化を証明するackではありません。
- flush timeoutはOTLP export HTTP timeoutとready待ちに使いますが、bootstrap時のhealth、login、current-user request timeoutは制御しません。
- 現行SDKのbootstrap requestは既定で最大60秒待ち得ます。
  connector daemonがruntime shutdown後もconstructor、startup replay、またはcleanupを続ける間は、保持したruntime参照の`health_snapshot()`で`connector_cleanup_deferred=true`になります。
- background flusherとdeferred processorは別々にflush timeoutを使うため、process全体のshutdown wall-clockを一つのtimeoutには収めません。

License: [Apache-2.0](LICENSE)

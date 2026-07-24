# Hermes AgentとGalileoを接続するための技術調査

- 調査基準日：2026-07-24
- 対象：Observability、OpenTelemetry、Hermes Agent、hermes-otel、Galileo
- 情報源：各プロジェクトの公式文書、仕様書、公式リポジトリ

## 調査の前提

このプロジェクトは、Hermes AgentのライフサイクルイベントをOpenTelemetryのトレースへ変換し、Galileoへ直接送信する。

hermes-otelが任意のOTLP互換バックエンドへ送る汎用プラグインであるのに対し、このプロジェクトはGalileo公式SDKを使うGalileo専用アダプターである。

調査結果には、公開仕様から確認できた事実と、現行実装から確認できた制約を含める。
OpenTelemetryのGenAI Semantic Conventionsは開発中であるため、現時点の属性名を将来も不変とは扱わない。

## Observabilityと分散トレーシング

システムの外部出力から内部状態を理解できる性質を**Observability**と呼ぶ。
OpenTelemetryは、trace、metric、logなどのテレメトリーを生成、処理、転送するためのベンダー中立な枠組みを提供する。
この定義と信号の位置づけは、[OpenTelemetryのObservability Primer](https://opentelemetry.io/docs/concepts/observability-primer/)に基づく。

AI Agentの一回の応答は、LLM推論、ツール実行、承認、サブエージェント委譲を含む。
平均応答時間やエラー件数だけでは、それらのどの処理が遅延または失敗したかを判定できない。
そこで、一回のユーザーターンをtrace、個別処理をspanとして親子関係を保存する。

別プロセスへ処理を委譲する場合は、trace IDと親span IDを伝播する必要がある。
HTTPを介する伝播には、[W3C Trace Context](https://www.w3.org/TR/trace-context/)の`traceparent`と`tracestate`を使う。
OpenTelemetryのcontext propagationの説明は、[OpenTelemetry Context Propagation](https://opentelemetry.io/docs/concepts/context-propagation/)にある。

## Hermes Agentのhookとtrust boundary

[Hermes Agent公式リポジトリ](https://github.com/NousResearch/hermes-agent)は、LLM、tool、session、gateway、subagentを一つのAgent processで扱う。
[Hermes Event Hooks](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/hooks.md)は、plugin hookをCLIとGatewayの両方で使える観測面として説明し、hook errorがAgentをcrashさせないことを契約に含めている。

hermes-galileoも同じfail-open境界を維持する。
callbackは追加fieldを許容し、観測側の失敗をAgent結果へ伝播せず、diagnostic logへevent payloadを書かない。

[Hermes Agent Security Policy](https://github.com/NousResearch/hermes-agent/security)によれば、pluginはAgent processへ読み込まれ、Agentと同じcredentialおよび権限へ到達できる。
pluginのinstallとenableはoperatorのtrust判断であり、Galileoへの外向き通信とcontent取得を別々に制御する必要がある。

installed pluginの`hermes_agent.plugins` entry pointは、登録関数そのものではなく、callableな`register`を公開するmodule objectを返す必要がある。
現行packageは`hermes_galileo = "hermes_galileo"`としてmoduleを公開し、installed distribution metadataから実際にloadするtestで契約を固定する。

## hermes-otelから継承する設計

[hermes-otel公式文書](https://briancaffey.github.io/hermes-otel/)は、Hermesのライフサイクルhookを親子関係のあるspanへ変換する。
同文書は、二種類の属性規約、バックエンドごとの非同期worker、ターン要約、コンテンツ非取得、放置spanのTTL終了を設計要素として挙げている。

このプロジェクトは、次の考え方を継承する。

- Hermesの観測hookを利用し、Agent本体の制御フローを変更しない。
- hookの追加フィールドを許容し、既知のフィールドだけを意味のある属性へ変換する。
- ユーザーターンをroot spanとし、LLM呼び出し、ツール実行、承認、サブエージェントをchild spanとする。
- exporterをユーザー応答の同期経路から分離する。
- contentを取得しなくても、token数、duration、model、tool名、statusなどの運用メタデータを送る。
- 終了イベントが欠けた状態をTTLで回収し、開いたspanを残し続けない。

一方、hermes-otelのmulti-backend fan-outは、このプロジェクトの既定要件ではない。
Galileo専用のproject、log stream、評価機能を使うため、公式Galileo span processorを一つの送信先として登録する。

## OpenTelemetry GenAI Semantic Conventions

OpenTelemetryは、GenAI推論、Agent、tool実行、token利用量の属性規約を定義している。
2026-07-24時点の[GenAI Semantic Conventionsリポジトリ](https://github.com/open-telemetry/semantic-conventions-genai)は、仕様のstatusをDevelopmentとしている。
実装はSDKと規約のversionを固定し、version更新時に属性契約を再検証する必要がある。

### 論理操作としてのspan

[GenAI spans仕様](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md)は、一回の論理的なGenAI操作を一つのspanとして記録する。
spanは要求開始から応答完了、errorまたはcancelまでを含み、内部の自動retryも論理spanのdurationに含む。

推奨するspanは次のとおりである。

| 処理 | span名 | kind | 主な属性 |
| --- | --- | --- | --- |
| ローカルAgent呼び出し | `invoke_agent {agent_name}` | `INTERNAL` | operation、provider、agent、conversation |
| リモートAgent呼び出し | `invoke_agent {agent_name}` | `CLIENT` | operation、provider、agent、server |
| LLM chat | `chat {request_model}` | `CLIENT` | provider、request model、response model、usage |
| tool実行 | `execute_tool {tool_name}` | `INTERNAL` | tool name、tool call ID、arguments、result |
| workflow | `invoke_workflow {workflow_name}` | `INTERNAL` | workflow name、input、output |

[Agent spans仕様](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md)では、`gen_ai.operation.name`と`gen_ai.provider.name`が基礎属性になる。
Agent名、request model、serverなど、head samplingに必要な低cardinality属性はspan開始時に設定する。

`gen_ai.conversation.id`には、実在する会話識別子だけを設定する。
仕様は、fallbackとしてUUIDを生成すること、trace IDを流用すること、要求内容のhashを使うことを認めていない。

### 入出力とtoken

入力、出力、system instruction、tool定義、tool arguments、tool resultは、大きく機微な値になり得る。
GenAI spans仕様はこれらをOpt-Inとし、既定では記録しない。
本番で内容を保存する必要がある場合は、アクセス制御と保持期間をテレメトリー本体から分離することも推奨している。

token利用量には、少なくとも次の属性を使う。

- `gen_ai.usage.input_tokens`
- `gen_ai.usage.output_tokens`
- `gen_ai.usage.cache_read.input_tokens`
- `gen_ai.usage.cache_creation.input_tokens`
- `gen_ai.usage.reasoning.output_tokens`

providerが消費token数と課金token数の両方を返す場合、標準属性には課金token数を優先する。
input totalはcached tokenを含み、reasoning tokenはoutput tokenに含まれるという定義をproviderの応答形式へ対応づける。

Hermesのcanonical usageでは、`prompt_tokens`がuncached inputとcache readおよびcache writeを含むaggregate inputを表す。
現行実装は`prompt_tokens`を`gen_ai.usage.input_tokens`へ設定し、cache readとcache creationも各専用属性へ設定する。
aggregate inputからcache tokenを再び加算しない。

[GenAI metrics仕様](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-metrics.md)は、operation duration、token usage、time to first token、agent inference call数、tool call数などを定義している。
token数はproviderから信頼できる値を取得できた場合だけ送る。

### errorの記録

[OpenTelemetryのerror記録規約](https://opentelemetry.io/docs/specs/semconv/general/recording-errors/)は、失敗したspanへ`ERROR` statusと低cardinalityの`error.type`を設定する。
成功時のstatusは明示的な`OK`ではなく未設定を推奨する。

内部で一度失敗してもretry後に論理操作が成功した場合、その論理spanをerrorにしない。
個々の試行を記録するなら、失敗した試行spanだけをerrorにする。
同じexceptionをstatus、event、複数属性へ重複して保存しない。

## GalileoのOpenTelemetryインターフェース

[Galileo SDK Overview](https://docs.galileo.ai/sdk-api/overview)は、SDKがAPI key、project、log stream、console URLを公式環境変数から取得すると説明している。
同文書でnative sessionの手動作成を担うのは`GalileoLogger`であり、OpenTelemetry processorの登録だけでsession provisioningを行うとは記載していない。

[GalileoのOpenTelemetry統合概要](https://docs.galileo.ai/sdk-api/third-party-integrations/opentelemetry-and-openinference)は、PythonとTypeScript向けの`GalileoSpanProcessor`を案内している。
同processorは、GalileoのcredentialからOTLP headerを作り、送信先を選択し、batch span processorを構成する。
認証とroutingには、`GALILEO_API_KEY`、`GALILEO_PROJECT`、`GALILEO_LOG_STREAM`を使う。
custom deploymentでは、console endpointに`GALILEO_CONSOLE_URL`、API endpointの明示pinに`GALILEO_API_URL`を使う。
`GALILEO_CONSOLE_URL`を設定する場合、hermes-galileoはprocess globalなSDK初期化raceでAPI routeを暗黙に継承しないよう`GALILEO_API_URL`も必須にする。

この公式processorを使えば、プロジェクト側でGalileo固有のheaderやOTLP endpointを再実装する必要がない。
自社運用版のGalileoでは`GALILEO_CONSOLE_URL`を使ってendpointを解決する。

### Galileoが有効とみなすspan

[Galileo OpenTelemetry Integration Recommendations](https://docs.galileo.ai/sdk-api/third-party-integrations/opentelemetry-and-openinference/integration-recommendations)は、span種別ごとの最小属性を示している。

- Agent spanには、operation、provider、適切なspan名とkind、input、outputが必要である。
- LLM spanには、operation、provider、model、input、outputが必要である。
- Tool spanには、`execute_tool`、tool名、arguments、resultが必要であり、tool call IDが推奨される。
- Error spanには、`error.type`と`ERROR` statusが必要である。

ここにはOpenTelemetryとの仕様上の緊張がある。
Galileoは有効なspanのためにinputとoutputを要求する一方、OpenTelemetryは実内容の記録をOpt-Inとしている。
そのため、既定では型を保ったprivacy placeholderを送り、明示的に許可された場合だけ秘匿済み内容を送る設計が必要になる。
placeholderがGalileoの表示と評価で有効かどうかは、live E2Eで検証する。

OTLP応答はHTTP 200でも一部のspanを拒否できる。
`partialSuccess.rejectedSpans`を完全成功として扱わず、拒否数を観測する必要がある。

### Session、trace、span

[Galileo Logging Basics](https://docs.galileo.ai/sdk-api/logging/logging-basics)は、sessionを論理的な会話、traceを一回のユーザーinteraction、spanを個別のLLM、tool、workflow処理として説明している。
現行実装はGalileo native sessionをprovisionせず、Hermes session IDを`gen_ai.conversation.id`へ設定して会話をgroupingする。
一回のHermes turnを一つのtraceへ対応づける。

一つの会話に複数のユーザーターンが含まれる場合、conversation IDを共有し、traceをターンごとに分ける。
サブエージェントは親ターンのchild spanとし、その内部処理をさらに子として接続する。
Galileo native sessionの作成、external ID設定、終了処理は将来拡張であり、現行の到達保証には含めない。

### Agent運用品質とcost

[Galileo Agentic Metrics](https://docs.galileo.ai/concepts/metrics/agentic/agentic-overview)は、Action Advancement、Action Completion、Agent Efficiency、Agent Flow、Conversation Quality、Tool Error、Tool Selection、Reasoning Coherenceなどを提供する。
これらはテレメトリー配送SLOとは別に、Agentの品質をmodel、prompt、agent versionごとに評価するために使う。

[Galileo Model Pricing Settings](https://docs.galileo.ai/concepts/costs/model-pricing-settings)は、登録したmodel単価とtoken数からcostを計算する。
このプロジェクトはmodel名と課金token数を正確に送ることを優先し、推定costを標準属性として捏造しない。
providerが実額を返す場合は、金額、通貨、算定元、pricing versionを独自namespaceで区別する必要がある。

## Privacyとsecurity

[OpenTelemetryのSensitive Dataガイド](https://opentelemetry.io/docs/security/handling-sensitive-data/)は、データ最小化と収集前の秘匿を実装者の責任としている。
promptだけでなく、tool arguments、tool result、retrieval document、URL query、exception message、metadataにもsecretや個人情報が入り得る。

このプロジェクトのprivacy境界は、spanへ属性を設定する前に置く。
API key、Authorization header、cookie、password、private keyなどのsensitive mapping keyを除去する。
自由textからは、Bearer token、generic JWT、既知API key形式、AWS `AKIA`または`ASIA` access key、Google `AIza` key、空白を含むquoted passwordまたはsecret assignment、PEM private key、`Cookie:`、`Set-Cookie:`を除去する。
base64 data URIは、文字列全体か自由text途中かにかかわらず省略する。
collectionの要素数、再帰depth、文字数も制限する。
payload全体をexport後に除去する方式では、SDK queueやdebug出力へ生データが残るため不十分である。

識別子のhash化は匿名化ではなく仮名化である。
特に低entropyのuser IDへ単純なSHA-256を使うと辞書攻撃が可能である。
安定した相関が必要な本番環境では、versionを付けたkeyed HMACとkey rotationを要件にする。

hidden chain-of-thoughtは収集しない。
content取得と会話履歴取得を有効にした場合も、既知のreasoning、thinking、analysis、encrypted reasoningのmapping keyは値を`[REDACTED REASONING]`へ置換する。
Anthropic形式の`thinking`と`redacted_thinking` blockは、`type`以外の`thinking`、`signature`、`data`を含む全fieldを同じplaceholderへ置換する。
Gemini形式の`thought_signature`も値を同じplaceholderへ置換する。
reasoning token数と、hidden reasoning用keyではない通常fieldへ明示された公開可能なplanまたはaction summaryだけを観測対象にする。

## Sampling、backpressure、retry

[OpenTelemetry Sampling](https://opentelemetry.io/docs/concepts/sampling/)は、head samplingとtail samplingを区別している。
低trafficの開発環境では100%を記録し、本番では`ParentBased`のratio samplerでtrace単位の一貫性を保つ。

高trafficでerror、高latency、高token利用のtraceを確実に残す必要がある場合は、Collectorのtail samplingを使う。
[Tail Sampling Processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/README.md)はstatefulであり、一つのtraceに属する全spanを同じCollectorへ送る必要がある。

[OTLP仕様](https://opentelemetry.io/docs/specs/otlp/)は、HTTP 429、502、503、504をretryableとして扱う。
`Retry-After`を尊重し、exponential backoffとjitterを使う。
認証、schema、routingの恒久errorはretryせず、partial successもretryしない。

しかし、現在Galileo SDKから使われる[OpenTelemetry Python 1.44のOTLP/HTTP exporter](https://github.com/open-telemetry/opentelemetry-python/blob/v1.44.0/exporter/opentelemetry-exporter-otlp-proto-http/src/opentelemetry/exporter/otlp/proto/http/trace_exporter/__init__.py)は、HTTP 200台をresponse bodyの解析前に成功として返す。
同versionの[retry判定](https://github.com/open-telemetry/opentelemetry-python/blob/v1.44.0/exporter/opentelemetry-exporter-otlp-proto-http/src/opentelemetry/exporter/otlp/proto/http/_common/__init__.py)は、connection errorに加えてHTTP 408と5xxだけを再送対象にする。
したがって、現在のdirect SDK構成は429をretryせず、`Retry-After`を尊重せず、HTTP 200の`partialSuccess`本文を検出できない。

OTLPは、送信成功後のackが失われた場合に重複を生じ得る。
retry時はtrace IDとspan IDを再生成せず、Galileoが常に重複排除すると仮定しない。

[OpenTelemetry Collector Resiliency](https://opentelemetry.io/docs/collector/resiliency/)は、sending queue、retry、永続WAL、外部message queueを耐久性の段階として説明している。
直接SDK構成は運用を単純にするが、永続buffer、tail sampling、集中redaction、exporter self-metricsが必要ならCollectorを追加する。

### 観測pipeline自身のtelemetry

観測pipelineの障害をAgent traceだけから判定することはできない。
spanをexportできない障害では、その障害を表すspanも同じ経路で失われるためである。

[OpenTelemetry SDK Metrics](https://opentelemetry.io/docs/specs/semconv/otel/sdk-metrics/)は、span processor queueのsize、capacity、処理数、queue-full error、exporterのin-flight数とexport数を定義している。
[Collector Internal Telemetry](https://opentelemetry.io/docs/collector/internal-telemetry/)は、accepted、refused、enqueue failure、send failure、queue size、queue capacityなどの内部metricを提供する。

hermes-galileoは、generated、accepted、exported、rejected、dropped、retry、queue oldest age、last successを低cardinality metricとして持つ必要がある。
現行のdeferred processorは、SDK接続前のbuffer量、drop数、接続試行数、最終接続error typeを公開する。
SDK接続後のqueue、export成否、partial rejection、queue oldest ageは公開しないため、必要ならprocessor wrapperまたはCollectorで取得する。

## 現行実装から確認できた状態

現行実装は、次の基礎要件を満たしている。

- Galileo公式span processorと、projectおよびlog streamの公式環境変数を使う。
- installed `hermes_agent.plugins` entry pointから、`register`を持つ`hermes_galileo` module objectをloadできる。
- 独立したTracerProviderへservice、version、environmentをresource属性として設定する。
- `ParentBased`のratio samplerを使う。
- hook例外をAgentへ伝播せず、payloadをlogへ出さない。
- Galileo SDK constructorが行うhealth check、login、current user取得をdaemon threadで実行し、Hermesのplugin登録をnetwork待ちでblockしない。
- process globalなGalileo SDK singletonのAPI key、console URL、API URLを構築前後に検証し、routing競合では作成したprocessorを閉じて`failed`にする。
- custom `GALILEO_CONSOLE_URL`を設定する場合は、trusted routeとして`GALILEO_API_URL`も必須にする。
- console URLまたはAPI URLを設定しない場合は、initialize時に同名のstale environment値を除去する。
- SDK接続前に終了したspanを最大2048件保持し、接続後に公式processorへreplayする。
- SDK初期化のHTTP 408、429、500以上と、`ImportError`、`TypeError`、`ValueError`以外のstatusなしerrorを1秒、5秒、30秒、60秒へjitterを加えた間隔で再試行する。
- SDK初期化の非retryableなstatus付きerror、`ImportError`、`TypeError`、`ValueError`、既存SDK singletonの設定競合では再試行を止め、保持中と以後のspanをdropとして数える。
- `connecting`、`replaying`、`ready`、`failed`、`stopping`、`stopped`を区別し、startup bufferのreplay完了後だけreadyにする。
- lockで保護したshutdown gateにより、shutdown開始後のhook受付を拒否する。
- 一回のターン、LLM API、tool、approval、subagentの親子spanを再構成する。
- session IDがないapprovalを一意なturn IDで相関し、preとpostの両方で`tool_call_id`をapproval identityとして優先して対応tool spanのchildにする。
- commandとpatternが同じparallel approvalも、異なる`tool_call_id`で分離する。
- canonicalなroot eventにproviderがない場合は、後続API eventのproviderをroot Agent spanへ補完する。
- 開始eventが欠けてもsynthesized spanを作り、終了eventが欠けたspanをTTLまたはshutdownで閉じる。
- 同時に保持するターン状態を512件に制限する。
- content取得を既定で無効にし、有効時もsecret redactionとcollection制限を適用する。
- content取得設定にかかわらず、既知のhidden reasoning key、Anthropic reasoning block全体、Gemini `thought_signature`を`[REDACTED REASONING]`へ置換する。
- base64 data URIを文字列全体と自由text途中の両方で省略する。
- API keyとpseudonym secretを`Settings`の`repr`から除外する。
- generic JWT、AWSとGoogleの既知access key形式、空白を含むquoted passwordまたはsecret assignmentを自由textから秘匿する。
- content取得だけではcanonical provider requestの過去履歴を送らず、会話履歴の独立Opt-Inが有効な場合だけ`body.messages`を使う。
- 会話履歴取得が有効なcanonical requestの`body.messages`とcanonical responseの`assistant_message`をstructured GenAI messagesとして取り出す。
- structured GenAI messagesが文字数上限を超えた場合は、有効なJSONの省略messageへ置き換える。
- mappingとsequenceの正規化では、要素上限までだけをiterateし、元collectionの長さから省略数を記録する。
- root Agent、LLM、Toolでは、開始eventが欠けたsynthesized spanを含め、OpenInference互換のinputとoutputおよび`text/plain` MIME typeを記録する。
- content取得が無効ならerror messageを保存せず、status descriptionには100文字以下のbounded error typeだけを設定する。
- content取得が有効なら、専用のerror message保存先をredactor後のexception eventへ集約し、重複するcustom error属性を作らない。
- Tool resultは別のoutput contractとして記録するため、explicit error messageがなくresultをexception messageへfallbackした場合は同じ本文を含み得る。
- `error.type`を100文字以下のbounded識別子へ正規化し、成功spanのstatusを未設定にする。
- Hermesのaggregate `prompt_tokens`をinput tokenへ設定し、cache tokenを別属性にも保持し、明示されたzero値を欠落させない。
- ユーザー識別子を既定で仮名化し、専用secretまたはGalileo API keyがある場合はHMAC-SHA-256を使う。
- 同じ論理API request IDを持つretryを別spanとして記録し、1始まりのattempt番号を付け、rootでは一意な論理request数だけを数える。
- ターン終了後のflushをbackground threadへ渡し、複数要求をまとめる。
- health snapshotで`exporter_ready`、`exporter_state`、`buffered_spans`、`dropped_spans`、`connection_attempts`、`last_connection_error_type`、`last_connection_error_retryable`、`retry_stopped_reason`、`connector_cleanup_deferred`、`provider_cleanup_deferred`、`delegate_cleanup_deferred`を取得する。
- 各shutdown段階でflush timeoutを使い、期限を超えるflush、Provider cleanup、delegate cleanupは互いに競合させずdaemonで継続する。
- `force_flush`がoperation lockを取得した後にready状態とdelegate identityを再検証し、shutdownとの競合時に停止済みdelegateを呼ばない。
- wire E2Eで公式SDKのhealth check、login、current user取得、OTLP protobuf、認証header、routing、parentage、token、履歴Opt-In、秘匿を検証する。
- wire failure matrixで401、429、503、408、connection reset、read timeout、partial success、large payloadを再現し、現行retry回数とretry時のtrace ID、span ID、request body保持を固定する。

ただし、次の項目は本番利用前の制約として残る。

- content modeは真偽値であり、`none`、`redacted`、`full`を明示的に区別しない。
- content取得を有効にしても既知secretは常に秘匿するため、実質的には`redacted` modeである。
- 専用のpseudonym secretがない場合はGalileo API keyをHMAC keyとして使うため、API key rotationと識別子の連続性が結合する。
- runtime外でsecretなしの仮名化関数を使う場合はsaltなしSHA-256へfallbackするため、低entropy識別子には使えない。
- structured GenAI messagesは有効なJSONを保つが、tool argumentsとtool resultの長大なJSON文字列はtextとして切り詰める。
- 2048件のstartup bufferはprocess memory上だけにあり、crash後には復元できない。
- buffer満杯時は最古spanをdropし、shutdown期限までSDKへ接続できない場合は残存spanをdropする。
- `dropped_spans`はstartup bufferのoverflow、replay失敗、恒久初期化失敗、failed、stopping、stopped状態で終了したspan、shutdown dropを数える。
- ready後にdelegateの`on_end`が失敗した場合はwarningを出すが、`dropped_spans`へ加算しない。
- `exporter_ready=true`はSDK初期化とstartup buffer replayの完了を表し、Galileoがその後のOTLP batchを受理したことまでは保証しない。
- SDK接続後のqueue量、最終送信成功、export latency、partial rejectionをhealth snapshotから判定できない。
- `error.type`の文字種と長さは制限するが許可語彙を強制しないため、低cardinalityはupstream taxonomyに依存する。
- custom `GALILEO_CONSOLE_URL`を設定する場合は、暗黙のAPI endpointを継承しないよう`GALILEO_API_URL`も明示する必要がある。
- `force_flush=True`は公式processorへqueue drainを要求できたことを表し、Galileoのdelivery acknowledgmentではない。
- background flusherとProvider配下のdeferred processorは別々にflush timeoutを使うため、runtime shutdown全体を一つのtimeoutに収める保証はない。
- 外部注入Providerの`force_flush`がtimeoutを無視する場合は同期shutdownが長引き得る。
- daemonへ延期したcleanupの完了は同deadline内に保証しない。
- flush timeoutはOTLP export HTTP timeoutへ渡すが、processor構築前のhealth check、login、current user取得のbootstrap HTTP timeoutは制御しない。
- 現行galileo-coreは各bootstrap requestで既定最大60秒待ち得る。
- connector daemonがruntime shutdown後もconstructor、startup replay、またはその後のcleanup内で継続する間は、`connector_cleanup_deferred=true`になる。
- processがdaemon cleanup完了前に強制終了すると未送信spanを復元できない。
- OpenTelemetry Python 1.44のdirect exporterは429と`Retry-After`のOTLP要件を満たさず、HTTP 200のpartial successを検出しない。
- 永続spool、tail sampling、Collector構成は実装していない。
- API request ID、tool call ID、turn IDが欠けた場合はbest-effortの識別子を作るため、並行または反復呼び出しで一意性を保証できない。
- TTL sweepはhook受信時に動くため、完全にidleなprocessでは次eventまたはshutdownまで回収されない。
- 実Galileo環境でのread-back、native session、Agentic Metricsの算出は外部credentialを使うlive E2Eまで保証しない。

これらは、[要件定義](./REQUIREMENTS.md)で受入基準と現在の充足状態へ対応づける。

## 調査から導いた設計判断

調査結果から、三つの判断を設計へ固定する。

1. Galileoへの既定送信には、公式`GalileoSpanProcessor`を使う。
2. prompt、response、tool arguments、tool result、会話履歴は明示Opt-Inとし、許可された場合も送信前に秘匿する。
3. 既定はdirect SDKとし、永続buffer、OTLP準拠retry、partial success検出、tail sampling、集中policy、詳細なexporter self-observabilityが必要な環境だけCollectorを追加する。

判断の理由、棄却案、影響は、[設計](./DESIGN.md)のADRに記載する。

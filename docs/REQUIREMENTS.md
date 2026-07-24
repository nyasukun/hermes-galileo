# Hermes AgentからGalileoへの連携要件

- 版：0.1
- 基準日：2026-07-24
- 対象リリース：hermes-galileo 0.1系

## 目的

Hermes Agentのユーザーターン、LLM呼び出し、tool実行、承認、サブエージェント委譲を、親子関係を保ったOpenTelemetry traceとしてGalileoへ送る。

観測処理はAgentの応答可否から切り離し、privacy、配送状態、schema互換性を検証可能にする。

## 適用範囲

適用範囲には、次の処理を含める。

- Hermesの観測hookからイベントを受ける。
- HermesイベントをOpenTelemetry GenAI属性へ変換する。
- Galileo公式SDKへspanを渡す。
- contentの取得可否、秘匿、上限を制御する。
- sampling、turn state、flush、shutdownを管理する。
- health情報と配送SLIを提供する。
- fake exporterとlive Galileoを使ってE2Eを検証する。

次の処理は既定の適用範囲に含めない。

- Galileoの評価modelや評価promptそのものの実装
- 複数のOTLPバックエンドへのfan-out
- Galileoの認証protocolまたはOTLP endpointの独自実装
- Galileo native sessionのprovisioningとlifecycle管理
- Agentのhidden chain-of-thoughtの収集
- Collectorの必須配備

## 技術制約

現行リリースは、次の制約内で設計する。

| ID | 制約 |
| --- | --- |
| CON-001 | runtimeはPython 3.10以上3.15未満を対象とする |
| CON-002 | Galileo連携は`galileo[otel]` 2.5.1以上3未満の公開APIへ依存する |
| CON-003 | OpenTelemetry GenAI Semantic ConventionsはDevelopment statusであり、属性互換性をversion更新時に再検証する |
| CON-004 | Galileoは有効なAgent、LLM、Tool spanへinputとoutputを要求するが、実contentはprivacy上Opt-Inにする |
| CON-005 | direct SDK構成ではstartup bufferを観測できるが、SDK接続後のqueue、OTLP retry、partial successの内部状態は取得できない |
| CON-006 | OTLPはend-to-endのexactly-once deliveryを保証せず、ack loss後のretryで重複が生じ得る |
| CON-007 | stableなturn、API request、tool callの相関はHermesが対応IDを供給することに依存する |
| CON-008 | 現行は`gen_ai.conversation.id`によるgroupingであり、Galileo native sessionの存在を前提にしない |
| CON-009 | 現在のOpenTelemetry Python 1.44 OTLP/HTTP exporterは408と5xxをretryするが、429と`Retry-After`を扱わず、HTTP 200のpartial success本文を解析しない |
| CON-010 | Galileo SDK設定はprocess globalなsingletonを使うため、既存instanceとAPI key、console URL、API URLが一致しなければ安全に共有できない |

## 要件の読み方

**必須**は本番リリースの受入に必要な要件を表す。
**推奨**は環境条件が成立する場合に適用する要件を表す。

現在状態は、2026-07-24時点の実装に対する非規範的な評価である。

- **充足**：記載した受入基準を現行実装で満たす。
- **一部充足**：主要動作はあるが、受入基準の一部が残る。
- **未充足**：実装または検証が残る。
- **外部依存**：Hermes、Galileoまたは運用基盤との組み合わせで確認する。

## インターフェース要件

| ID | 優先度 | 要件 | 受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| INT-001 | 必須 | Hermesの観測hookを使い、Agent本体の結果を変更しない | installed entry pointがcallableな`register`を持つmoduleを返し、全hook callbackが任意の追加fieldを受け入れ、observer内部の例外がAgent呼び出し元へ伝播しない | 充足 |
| INT-002 | 必須 | Galileo公式の認証とrouting設定を使う | 有効時は`GALILEO_API_KEY`、`GALILEO_PROJECT`、`GALILEO_LOG_STREAM`を要求し、custom `GALILEO_CONSOLE_URL`を指定した場合は`GALILEO_API_URL`も要求して両方を公式SDKへ渡す | 充足 |
| INT-003 | 必須 | operatorが連携を無効化できる | `HERMES_GALILEO_ENABLED=false`または`GALILEO_LOGGING_DISABLED=true`でruntimeを開始せず、Agentは通常動作する | 充足 |
| INT-004 | 必須 | 会話、ターン、API要求、tool callを安定したIDで相関する | Hermesが`session_id`、`turn_id`、`api_request_id`、`tool_call_id`を供給した場合、全spanで同じ値を保持し、並行呼び出しが交差しない | 充足 |
| INT-005 | 必須 | 欠落した開始eventを安全に補完する | 終了eventだけを受けた場合にsynthesized spanを作り、processを失敗させず、補完した事実を属性で判別できる | 充足 |
| INT-006 | 必須 | subagentを親ターンへ接続する | parent sessionとchild sessionが指定されたとき、subagent spanが親ターンのchildとなり、その配下へchild sessionの処理が接続される | 充足 |
| INT-007 | 推奨 | remote serviceへtrace contextを伝播する | HTTPなどのprocess境界を越える経路でW3C `traceparent`を挿入し、受信側のSERVER spanが同じtraceに属する | 未充足 |

INT-004の完全な一意性は、Hermesがstable IDを供給することを前提とする。
fallback IDはevent欠落時の継続動作を目的とし、反復する同名tool callの一意性を保証しない。

## Traceとschemaの要件

| ID | 優先度 | 要件 | 受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| TEL-001 | 必須 | 一回のユーザーターンを一つのtraceとして表す | root spanが`invoke_agent {agent_name}`、kindがローカル呼び出しでは`INTERNAL`、operationが`invoke_agent`になる | 充足 |
| TEL-002 | 必須 | 一つの会話へ複数traceを関連づける | 実在するHermes session IDを`gen_ai.conversation.id`へ設定し、trace IDや生成UUIDを代用しない | 充足 |
| TEL-003 | 必須 | LLM呼び出しをGenAI spanとして表す | span名が`chat {request_model}`、kindが`CLIENT`となり、operation、provider、request model、input、outputを持つ | 充足 |
| TEL-004 | 必須 | tool実行をGenAI spanとして表す | span名が`execute_tool {tool_name}`、kindが`INTERNAL`となり、operation、tool名、call ID、arguments、resultを持つ | 充足 |
| TEL-005 | 必須 | approvalとsubagentをtraceへ含める | session IDがないapprovalを一意なturn IDへ相関し、preとpostの`tool_call_id`でparallel approvalを対応tool spanへ接続し、subagentの開始と終了を一つのAgent spanにする | 充足 |
| TEL-006 | 必須 | providerが返したtoken利用量を失わない | Hermesのaggregate `prompt_tokens`をinputへ設定し、output、cache read、cache creation、reasoningをzeroを含む同じ整数で各専用属性へ設定する | 充足 |
| TEL-007 | 必須 | request modelとresponse modelを区別する | providerが実際のresponse modelを返した場合、`gen_ai.response.model`へ保存し、request modelを上書きしない | 充足 |
| TEL-008 | 必須 | errorを規約どおり記録する | 各API試行を別spanにし、失敗試行だけが`ERROR`と`error.type`を持ち、成功試行のstatusは未設定となり、同じ論理要求IDと1始まりのattempt番号でretryを相関できる | 一部充足 |
| TEL-009 | 推奨 | providerの実costを独自属性で区別する | 実額がある場合にamount、currency、source、pricing versionを持ち、推定値と実額を同じfieldへ入れない | 未充足 |
| TEL-010 | 必須 | GalileoとOpenInferenceの表示互換性を保つ | Galileoのminimum valid spanを満たし、必要な`gen_ai.*`と互換用OpenInference属性がlive画面で解釈される | 外部依存 |
| TEL-011 | 必須 | semantic conventions更新を管理する | Galileo SDK、OpenTelemetry SDK、GenAI schemaのversionを記録し、更新時にgolden contract差分を承認する | 一部充足 |
| TEL-012 | 必須 | span名とdimensionを低cardinalityに保つ | user input、user ID、session ID、URL、tool argumentsがspan名またはmetric labelへ入らない | 充足 |

TEL-006では、aggregate inputへcache tokenを再加算しない。
Hermes canonical usage外で課金token数と消費token数を別々に返すproviderを追加する場合は、新しいmapping契約が必要になる。
TEL-003とTEL-004はsynthesized経路を含めてGenAI inputとoutputの受入基準を満たす。
post eventだけから作るsynthesized LLMとTool spanも、privacy placeholderまたは取得可能なargumentから`input.value`、`input.mime_type=text/plain`、OpenInference span kindを補完する。
TEL-008は成功statusを未設定にし、terminal error typeを低cardinality向けのbounded識別子へ正規化する。
retryでは同じ`hermes.api.request_id`を維持し、試行spanへ1始まりの`hermes.api.attempt`を設定する。
rootの`hermes.turn.api_call_count`は試行数ではなく、一意な論理request ID数を数える。
現行の`error.type`は文字種と100文字上限を強制するが許可語彙を強制しないため、低cardinality taxonomyの実装も残る。
content取得有効時は、Toolを含め、専用のerror message保存先を秘匿後のexception eventへ集約し、custom error属性を作らない。
Tool resultは別のoutput contractとして残るため、explicit error messageがなくresultをexception messageへfallbackした場合は同じ本文を含み得る。

## Privacyとsecurityの要件

| ID | 優先度 | 要件 | 受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| PRI-001 | 必須 | content取得を明示Opt-Inにする | 未設定時はprompt、response、tool arguments、tool result、approval command、subagent goalを実内容ではなくplaceholderとして送る | 充足 |
| PRI-002 | 必須 | 会話履歴を別のOpt-Inにする | content取得だけを有効にしてもconversation history全体は送らず、専用設定を有効にした場合だけ対象にする | 充足 |
| PRI-003 | 必須 | contentをexport前に秘匿する | sensitive key、Bearer token、generic JWT、一般的なAPI key形式、AWSとGoogleの既知access key、quoted passwordまたはsecret、cookie、private key、base64 data URIがspan属性へ入る前に置換される | 充足 |
| PRI-004 | 必須 | 全contentの大きさを制限する | collection要素数、再帰depth、文字数の上限を超えた値を明示的な省略表現へ変え、memoryを無制限に使用しない | 充足 |
| PRI-005 | 必須 | GenAI messageのJSON構造を保って切り詰める | canonical request `body.messages`とresponse `assistant_message`が上限を超えても、有効なJSONの省略messageになり、`gen_ai.input.messages`と`gen_ai.output.messages`を解析できる | 充足 |
| PRI-006 | 必須 | user IDを既定で仮名化する | 既定ではraw IDを送らず、productionではversion付きkeyed HMACを使い、keyをtelemetryへ出さない | 一部充足 |
| PRI-007 | 必須 | error情報にも同じ秘匿規則を適用する | content取得無効時はmessageを保存せず、status descriptionには100文字以下のbounded error typeだけを設定し、有効時だけredactor後のexception eventを保存する | 充足 |
| PRI-008 | 必須 | secretを設定とhealthから漏らさない | API keyがspan、log、health snapshot、例外、設定dumpのいずれにも現れない | 一部充足 |
| PRI-009 | 必須 | tenant routingをtrusted configへ限定する | projectとlog streamをユーザー入力から上書きできず、tenant Aのtraceがtenant Bへ到達しない | 一部充足 |
| PRI-010 | 必須 | hidden chain-of-thoughtを収集しない | contentと会話履歴の取得設定にかかわらず、既知のhidden reasoning key、Anthropic reasoning block、Gemini `thought_signature`を`[REDACTED REASONING]`へ置換し、reasoning token数と通常fieldへ明示された公開可能なsummaryだけを送る | 充足 |
| PRI-011 | 必須 | data governanceを運用で定める | Galileo側のRBAC、retention、削除、region、API key rotationの責任者と期限が記録される | 外部依存 |

PRI-004はmappingとsequenceの全体をlist化せず、`islice`で設定上限までだけを読み、元collectionの長さから省略数を記録する。
PRI-005の保証対象はGenAI message属性である。
長大なtool argumentsとtool resultはbounded textとして扱い、JSON構造の保持を保証しない。
PRI-006はkeyed HMACを利用できるが、key versionの記録とrotation期間をまたぐ相関方針が残る。
専用secretもGalileo API keyも渡さずに仮名化関数を使うfallbackでは、低entropy IDに対する辞書攻撃を防げない。
PRI-002はroot inputとLLM API inputの両方へ適用する。
LLM APIでは、履歴取得が無効ならcanonical provider requestの`body.messages`を使わず、hookが別fieldで渡す現在の`user_message`だけを送る。
履歴取得が有効な場合だけ、canonical provider requestの`body.messages`をstructured inputとして送る。
PRI-003はsensitive mapping key、Bearer token、既知API key形式、assignment形式secret、PEM private key、Cookie header、文字列全体または自由text途中のbase64 data URIを置換する。
PRI-009はSDK singletonのAPI key、console URL、API URLを構築前後に検証し、未指定のstale environment値を除去する。
custom console URLを使う場合はAPI URLの明示pinを要求するが、tenant間のlive routing検証は残る。
PRI-008はAPI keyとpseudonym secretを`Settings`の`repr`から除外し、unit testで実値が出ないことを検証する。
任意のapplication log、third-party exception、設定dumpを横断する検証は運用受入として残る。

## Reliabilityとperformanceの要件

| ID | 優先度 | 要件 | 受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| REL-001 | 必須 | observability障害をAgent経路から隔離する | hook、span生成、flush、exportの例外がAgentの応答を失敗させず、payloadをerror logへ出さない | 充足 |
| REL-002 | 必須 | exportをユーザー応答の同期経路から外す | Galileo SDKのhealth check、login、current user取得とspan exportをAgentの同期経路から外し、turn-end flushをbackgroundで実行する | 充足 |
| REL-003 | 必須 | trace単位でsamplingを一貫させる | sample rateが0から1の範囲で検証され、`ParentBased` samplerが親のsampling decisionを継承する | 充足 |
| REL-004 | 必須 | in-flight stateを有界にする | 同時turnが512件を超えた場合に最古のturnをerror付きで終了し、process memoryを無制限に増加させない | 充足 |
| REL-005 | 必須 | stale stateを回収する | 設定TTLを超えたturnと未終了child spanを終了し、timed outまたはabandonedを判別できる | 充足 |
| REL-006 | 必須 | shutdown時に有界時間でflushする | 新規hook受付を止め、open spanを終了し、一つの全体deadline内でruntime shutdownを返し、超過するflushまたはSDK cleanupをdaemonで直列継続してhealthへ公開する | 一部充足 |
| REL-007 | 必須 | retryable errorだけを再送する | 429、502、503、504と一時network errorだけをjitter付きで再送し、`Retry-After`を尊重する | 未充足 |
| REL-008 | 必須 | partial successをlossとして観測する | HTTP 200の`partialSuccess.rejectedSpans`を成功数へ含めず、拒否span数と理由をmetricへ出し、再送しない | 未充足 |
| REL-009 | 必須 | exporter queueを有界にする | startup bufferのcapacity、現在量、drop数とfull時の方針を取得し、SDK接続後のqueue capacity、現在量、最古age、queue-full drop数も取得できる | 一部充足 |
| REL-010 | 必須 | retryでIDを変えない | connection resetまたはack loss後のretryでもtrace IDとspan IDが一致し、duplicate候補を観測できる | 一部充足 |
| REL-011 | 推奨 | 高耐久環境で永続bufferを使う | crash後も設定したmax bytesとmax ageの範囲で未送信spanを再送し、spoolを暗号化する | 未充足 |
| REL-012 | 推奨 | 高volume環境でtail samplingを使う | error、高latency、高token、canaryを保持し、小率の無作為baselineを残すpolicyをCollectorへ設定する | 未充足 |
| REL-013 | 必須 | Galileo SDK初期化を遅延実行する | daemon threadでSDKを初期化し、完了spanを最大2048件bufferし、一時失敗を1秒、5秒、30秒、60秒へjitterを加えた間隔で再試行し、replay完了後だけreadyにする | 充足 |

REL-006はSDK接続中のshutdownでflush timeoutまでreadyを待ち、OTLP export HTTP timeoutにも同じ値を渡す。
processor構築前のhealth check、login、current user取得にはgalileo-core側の既定timeoutが使われ、同設定では制御しない。
shutdown後のhookはlockで保護したgateが拒否する。
background flushまたはdelegate cleanupが期限を超える場合はdaemonで直列継続し、`provider_cleanup_deferred`または`delegate_cleanup_deferred`で判別できる。
constructor内のbootstrapまたはstartup replayがdeadlineを超えてconnector daemon上で継続する場合は、runtimeを有界時間で停止状態へ遷移させ、`connector_cleanup_deferred=true`で判別できる。
ただし、background flusherとProvider配下のdeferred processorが別々にflush timeoutを使うため、runtime全体のwall-clockを一つのtimeoutに収める保証はない。
外部注入Providerの`force_flush`がtimeoutを無視する場合もdaemonへ移さない。
`force_flush=True`はqueue drain要求の完了だけを表し、Galileoのdelivery acknowledgmentではない。
REL-007とREL-008は、現在のOpenTelemetry Python 1.44 exporterの実装を確認した結果である。
同exporterはconnection error、408、5xxをretryするが、429と`Retry-After`を扱わず、2xx response bodyを解析しない。
REL-009は2048件のstartup bufferとdrop counterを満たすが、公式Galileo processor内部のqueueとqueue ageを取得しない。
REL-010は503、408、connection resetのwire retryでtrace ID、span ID、serialize済みrequest bodyの保持を検証する。
送信先が受理した後にackだけを失う実環境の重複候補は、Galileo read-backでの検証が残る。
REL-013はHTTP 408、429、5xxと、`ImportError`、`TypeError`、`ValueError`以外のstatusなしerrorを再試行する。
その他のstatus付きerror、`ImportError`、`TypeError`、`ValueError`、既存SDK singletonの設定競合では`failed`へ遷移して再試行を止める。
必要なSLIをprocessorから取得できない場合は、wrapperまたはCollectorで補う。

## 運用要件

| ID | 優先度 | 要件 | 受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| OPS-001 | 必須 | 設定値をstartup時に検証する | boolean、integer、floatの不正形式と範囲外を具体的な設定名付きで拒否する | 充足 |
| OPS-002 | 必須 | processの状態をsecretなしで取得する | enabled、routing、capture、sample rate、in-flight件数に加え、exporter state、buffer量、drop数、接続試行数、最終接続error type、retry可否、停止理由、connector、Provider、ready delegateのdeferred cleanupを取得し、API keyを含まない | 充足 |
| OPS-003 | 必須 | livenessとdependency状態を分離する | Galileo outageだけではlivenessを失敗させず、queue saturationや受付不能をreadinessまたはstatusで判別できる | 一部充足 |
| OPS-004 | 必須 | exporterのself-observabilityを提供する | startup connection、buffer、dropに加え、accepted、exported、rejected、OTLP retry、SDK queue、export latency、last successを低cardinality metricで取得できる | 一部充足 |
| OPS-005 | 必須 | serviceとdeploymentを識別する | resourceにservice name、service version、deployment environment、plugin名が付く | 充足 |
| OPS-006 | 必須 | configuration revisionを識別する | secretを含まない設定revisionとSDK versionをhealthまたはresourceから取得できる | 一部充足 |
| OPS-007 | 必須 | privacy incidentを即時停止できる | operatorが一つの設定で連携を無効化し、既存credentialをrotationし、Galileo上の対象traceを特定できる | 外部依存 |
| OPS-008 | 推奨 | SLOのerror budgetを監視する | delivery、freshness、loss、overhead、privacyのSLIを複数windowのburn rateでalertできる | 未充足 |

運用手順と初期SLOは、[運用設計](./OPERATIONS.md)に記載する。

## TestとE2Eの要件

| ID | 優先度 | 要件 | 受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| TST-001 | 必須 | Hermes eventからspanへの契約を固定する | canonical eventごとにspan名、kind、parent、status、属性名、型、値をgolden fixtureで比較する | 充足 |
| TST-002 | 必須 | 並行eventを分離する | 複数session、複数turn、同名tool、同一commandとpatternのparallel approval、parallel subagentを交差させてもparentageとIDが混ざらない | 充足 |
| TST-003 | 必須 | privacy corpusを検証する | sensitive key、token、cookie、password、private key、data URI、再帰構造、巨大payload、error messageでcanaryが一度もexportされない | 充足 |
| TST-004 | 必須 | OTLP failure matrixを検証する | retryable、non-retryable、partial success、timeout、connection reset、large payloadをfake endpointで再現し、現行retry回数、health上のloss可視性、retry時のIDとbody保持をassertする | 充足 |
| TST-005 | 必須 | overloadとshutdownを検証する | queue capacity超過、512 turn超過、TTL、SIGTERM、flush timeout、crash recoveryでAgentが停止せず、lossを計測できる | 一部充足 |
| TST-006 | 必須 | live Galileoでtrace構造を検証する | 専用projectとlog streamへ送信し、session、trace、span parentage、model、token、finish reason、tool、errorをAPIまたは画面で確認する | 外部依存 |
| TST-007 | 必須 | Galileo privacyをread-backで検証する | synthetic PIIとcanary secretを送信経路へ投入し、Galileoのspan、metadata、error、logのいずれにも存在しない | 外部依存 |
| TST-008 | 必須 | routing分離を検証する | 誤ったkey、project、log streamを拒否し、二つのtenant fixtureが相互のprojectへ現れない | 外部依存 |
| TST-009 | 必須 | SDKとschemaの互換性を検証する | support対象の最小versionと最新versionで同じcontract suiteを実行し、差分をrelease前に承認する | 未充足 |
| TST-010 | 推奨 | GalileoのAgentic Metricsを検証する | 決定的なtool errorと完了fixtureで、対象metricが想定したspanまたはtraceへ計算される | 外部依存 |
| TST-011 | 必須 | 公式SDKのwire経路を検証する | stub Galileoに対するhealth check、login、current user取得、OTLP protobuf、認証header、routing、parentage、token、secret非混入を一つのE2Eで確認する | 充足 |

TST-001とTST-002の現在状態は、既存の自動検証が示す範囲に限る。
installed distribution metadataのentry pointを実際にloadし、module objectと`register`を検査するunit testも実行する。
TST-003はmapping key、Bearer、generic JWT、既知API key、AWSとGoogleのaccess key、quoted assignment secret、PEM private key、Cookie header、文字列全体と自由text途中のdata URI、hidden reasoning key、Anthropic reasoning block、Gemini `thought_signature`、再帰、文字数上限、巨大sequenceのiteration上限、error messageをunit testで検証する。
PEM private key、Cookie header、自由text途中のdata URI、各形式のhidden reasoning canary非混入はwire E2Eでも検証する。
同一論理API requestの失敗とretry成功を連続させるintegration testは、request IDの維持、1始まりのattempt番号、失敗と成功のstatus、rootの論理request数を検証する。
TST-004は公式SDKからstub endpointまでのwireで401、429、503、408、connection reset、read timeout、partial success、large payloadを再現する。
現行exporterが429をretryせず、partial successをhealthへ反映しない既知のgapも期待値として固定するため、TST-004の充足はREL-007とREL-008の充足を意味しない。
TST-011は公式SDKからstub endpointまでの通常時wire contractを検証する。
実Galileoでのread-back、表示、評価はTST-006からTST-010の外部依存または未充足として残る。

## 将来要件

次の要件は0.1系の受入対象ではなく、導入判断を別途行う。

| ID | 優先度 | 要件 | 将来の受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| FUT-001 | 将来 | Galileo native sessionをprovisionする | Hermes session IDを冪等なexternal IDへ対応づけ、作成、再開、終了、reset、重複、retryを定義し、失敗時もtrace exportを継続する | 未充足 |
| FUT-002 | 将来 | Collector profileを提供する | standard OTLP exporter、Collectorの認証、tail sampling、WAL、queue metricsをdirect profileと同じspan contractで検証する | 未充足 |

現行の会話相関はFUT-001へ依存しない。
同じHermes session IDを`gen_ai.conversation.id`へ設定した複数traceを、Galileo上でgroupingする。

## 初期受入ゲート

本番リリース候補は、次の条件をすべて満たす必要がある。

1. 必須要件の「未充足」が解消されるか、期限、owner、軽減策を持つ例外として承認されている。
2. TST-003のcanary secretが全経路で0件である。
3. TST-006で一つのsessionに複数traceが表示され、各traceのparentageと必須属性が一致する。
4. TST-004でpartial success、429、401、connection resetを区別できる。
5. Agent経路の同期overheadが[運用設計](./OPERATIONS.md)のSLO候補を満たす。
6. SDKとGenAI Semantic Conventionsのversionがrelease artifactへ記録されている。

## 外部仕様

要件は次の一次情報を基準にする。

- [Hermes Agent公式リポジトリ](https://github.com/NousResearch/hermes-agent)
- [Hermes Event Hooks](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/hooks.md)
- [Hermes Agent Security Policy](https://github.com/NousResearch/hermes-agent/security)
- [Hermes-otel公式文書](https://briancaffey.github.io/hermes-otel/)
- [OpenTelemetry GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md)
- [OpenTelemetry GenAI Agent spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md)
- [OpenTelemetry error recording](https://opentelemetry.io/docs/specs/semconv/general/recording-errors/)
- [OpenTelemetry OTLP仕様](https://opentelemetry.io/docs/specs/otlp/)
- [OpenTelemetry Python 1.44 OTLP/HTTP exporter](https://github.com/open-telemetry/opentelemetry-python/blob/v1.44.0/exporter/opentelemetry-exporter-otlp-proto-http/src/opentelemetry/exporter/otlp/proto/http/trace_exporter/__init__.py)
- [OpenTelemetry Python 1.44 retry判定](https://github.com/open-telemetry/opentelemetry-python/blob/v1.44.0/exporter/opentelemetry-exporter-otlp-proto-http/src/opentelemetry/exporter/otlp/proto/http/_common/__init__.py)
- [OpenTelemetry sensitive data](https://opentelemetry.io/docs/security/handling-sensitive-data/)
- [Galileo SDK概要](https://docs.galileo.ai/sdk-api/overview)
- [Galileo OpenTelemetry統合概要](https://docs.galileo.ai/sdk-api/third-party-integrations/opentelemetry-and-openinference)
- [Galileo OpenTelemetry推奨事項](https://docs.galileo.ai/sdk-api/third-party-integrations/opentelemetry-and-openinference/integration-recommendations)

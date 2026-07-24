# Hermes AgentからGalileoへの連携要件

- 版：0.1
- 基準日：2026-07-24
- 対象リリース：hermes-galileo 0.1系

## 目的

Hermes Agentのユーザーターン、LLM呼び出し、tool実行、承認、サブエージェント委譲を、親子関係を保ったOpenTelemetry traceとしてGalileoへ送る。

同じHermes会話に属する複数ターンを一つのGalileo native Sessionへ対応づけ、Session単位の会話評価を利用できる状態にする。

観測処理はAgentの応答可否から切り離し、privacy、配送状態、schema互換性、Session対応を検証可能にする。

## 適用範囲

適用範囲には、次の処理を含める。

- Hermesの観測hookからイベントを受ける。
- HermesイベントをOpenTelemetry GenAI属性へ変換する。
- Galileo公式SDKへspanを渡す。
- Galileo公式SDKでnative Sessionを作成または再利用し、Hermes sessionと対応づける。
- contentの取得可否、秘匿、上限を制御する。
- sampling、turn state、flush、shutdownを管理する。
- adapterが所有するlocal healthを提供し、end-to-end配送はGalileo read-backまたはCollectorの配送SLIで検証する。
- fake exporterとlive Galileoを使ってtrace、native Session、評価metricをE2Eで検証する。

次の処理は既定の適用範囲に含めない。

- Galileoの評価modelや評価promptそのものの実装
- 複数のOTLPバックエンドへのfan-out
- Galileoの認証protocolまたはOTLP endpointの独自実装
- Agentのhidden chain-of-thoughtの収集
- Collectorの必須配備
- direct profile内での永続WAL、tail sampling、OTLP data-plane exporterの独自retry

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
| CON-008 | `GalileoSpanProcessor`だけではnative Sessionを作成しないため、公式`GalileoLogger.start_session(external_id=...)`による制御面と、返却IDを`galileo.session.id`へ設定するOTLP data planeを併用する |
| CON-009 | 現在のOpenTelemetry Python 1.44 OTLP/HTTP exporterは408と5xxをretryするが、429と`Retry-After`を扱わず、HTTP 200のpartial success本文を解析しない |
| CON-010 | Galileo SDK設定はprocess globalなsingletonを使うため、既存instanceとAPI key、console URL、API URLが一致しなければ安全に共有できない |
| CON-011 | GalileoのConversation QualityはSession単位でtraceのinputとoutputを評価するため、placeholderだけを送る既定profileでは有用な評価結果を前提にできない |
| CON-012 | native Session timeoutはlocal mappingとpending spanをfail-open解放するdeadlineであり、公式SDK内で実行中の同期`start_session()` callをcancelしない |
| CON-013 | Galileo SDKのcredentialとroutingはprocess globalであるため、v1は一つのprocessで一つのHermes profileだけを観測し、multiplexされた別profileのeventは誤配送を避けるためdropする |

## 要件の読み方

**必須**は本番リリースの受入に必要な要件を表す。
**推奨**は環境条件が成立する場合に適用する要件を表す。

現在状態は、2026-07-24時点の実装に対する非規範的な評価である。

- **充足**：記載した受入基準を現行実装で満たす。
- **一部充足**：主要動作はあるが、受入基準の一部が残る。
- **未充足**：実装または検証が残る。
- **外部依存**：Hermes、Galileoまたは運用基盤との組み合わせで確認する。
- **範囲外**：v1のdirect profileでは実装せず、別のdeployment profileが責任を持つ。

## インターフェース要件

| ID | 優先度 | 要件 | 受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| INT-001 | 必須 | Hermesの観測hookを使い、Agent本体の結果を変更しない | installed entry pointがcallableな`register`を持つmoduleを返し、全hook callbackが任意の追加fieldを受け入れ、observer内部の例外がAgent呼び出し元へ伝播しない | 充足 |
| INT-002 | 必須 | Galileo公式の認証とrouting設定を使う | 有効時は`GALILEO_API_KEY`、`GALILEO_PROJECT`、`GALILEO_LOG_STREAM`を要求し、custom `GALILEO_CONSOLE_URL`を指定した場合は`GALILEO_API_URL`も要求して両方を公式SDKへ渡す | 充足 |
| INT-003 | 必須 | operatorが連携を無効化できる | `HERMES_GALILEO_ENABLED=false`または`GALILEO_LOGGING_DISABLED=true`でruntimeを開始せず、Agentは通常動作する | 充足 |
| INT-004 | 必須 | 会話、ターン、API要求、tool callを安定したIDで相関する | Hermesが`session_id`、`turn_id`、`api_request_id`、`tool_call_id`を供給した場合、process内では同じidentityを保持し、exportするsession identityにはSES-005のHMAC仮名値を使い、並行呼び出しが交差しない | 充足 |
| INT-005 | 必須 | 欠落した開始eventを安全に補完する | 終了eventだけを受けた場合にsynthesized spanを作り、processを失敗させず、補完した事実を属性で判別できる | 充足 |
| INT-006 | 必須 | subagentを親ターンへ接続する | parent sessionとchild sessionが指定されたとき、subagent spanが親ターンのchildとなり、その配下へchild sessionの処理が接続される | 充足 |
| INT-007 | 推奨 | remote serviceへtrace contextを伝播する | HTTPなどのprocess境界を越える経路でW3C `traceparent`を挿入し、受信側のSERVER spanが同じtraceに属する | 未充足 |
| INT-008 | 必須 | hermes-otelと同じprofile-local設定面を提供する | active `$HERMES_HOME/plugins/hermes_galileo/config.yaml`から非secretな挙動fieldだけを読み、空でない環境変数、YAML、組み込み既定値の順で解決し、空の環境変数はYAMLを消さず、secretまたはrouting fieldと不正YAMLはobservabilityだけを無効化する | 充足 |
| INT-009 | 必須 | multiplexed profile間の誤配送を防ぐ | runtimeを初期化時のactive Hermes homeへbindingし、別profile contextのhook eventをspanまたはSession APIへ渡さず、`profile_scope_mismatches`で件数を確認できる | 充足 |

INT-004の完全な一意性は、Hermesがstable IDを供給することを前提とする。
fallback IDはevent欠落時の継続動作を目的とし、反復する同名tool callの一意性を保証しない。
INT-006のcanonicalなevent順序は、`subagent_start`の後にchild sessionとchild turnのeventが続く形である。
child eventが先着しても未exportのspanはparent native Sessionへ再束縛するが、すでにexport済みのspanと開始済みのchild Session API requestは取り消せない。
v1で複数profileを同時に観測する場合はprofileごとにHermes processを分ける。
一つのmultiplexed processへprofile別Galileo routingを同居させる構成は対応しない。

## Sessionと会話評価の要件

| ID | 優先度 | 要件 | 受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| SES-001 | 必須 | Hermes sessionをGalileo native Sessionへ明示的に対応づける | 公式`GalileoLogger(project, log_stream).start_session(external_id=...)`を使い、Hermes session IDから作る`hermes:`接頭辞付きHMAC仮名値を`external_id`へ渡し、raw Hermes session IDをGalileoへ送らない | 充足 |
| SES-002 | 必須 | 一つの会話に含まれる複数ターンを同じnative Sessionへ入れる | `start_session`の返却UUIDを`galileo.session.id`として同じHermes sessionに属する全traceとspanへ設定し、各ターンは別traceのまま同じnative Sessionへ表示される | 充足 |
| SES-003 | 必須 | Session lifecycleをHermes lifecycleへ対応づける | `on_session_start`または最初の会話eventで作成または再利用し、`on_session_end`では対応を維持し、実行中またはqueue待ちのsubagent delegationがあれば`subagent_stop`までlocal mappingとaliasの解放を遅延し、解決中なら同じgenerationの成功、timeout、cancel callback後に破棄し、同じexternal IDの再開時はrelease予約を取り消して公式SDKの既存Session再利用を使う | 充足 |
| SES-004 | 必須 | Session API障害をAgentとtrace生成から隔離する | Session解決を二つのdaemon worker、512 mapping、512 job、4096 pending-ended-spanで有界化し、同じHermes sessionはsingle-flightにし、各turnのchild spanはrootが選んだgenerationを継承し、timeoutまたは失敗時はturn全体をSession属性なしでfail-open exportし、各状態をsecretなしで観測できる | 充足 |
| SES-005 | 必須 | Session相関IDにもprivacy境界を適用する | `external_id`、`gen_ai.conversation.id`、`hermes.session.id`にはtop-level Hermes sessionの同じHMAC仮名値を使い、subagent child固有HMACはsubagent相関属性だけに保持し、HMAC keyは専用pseudonym secretを優先して未設定時だけGalileo API keyへfallbackする | 充足 |
| SES-006 | 必須 | Session単位のConversation Qualityを利用できる | v1に会話評価profileを用意し、同profileではcontent取得を明示Opt-Inにしてprivacy審査済みのtrace inputとoutputを持つ二ターン以上のnative Sessionを送信でき、Galileo log streamでConversation Qualityを有効化できる | 外部依存 |

Galileo公式SDKは、同じ`external_id`のSessionを検索して既存Sessionを再利用する。
この動作はprocess再起動後の対応再開に使う。
複数processが同時に未作成のexternal IDを解決した場合の一意性はSDKとGalileo APIの挙動に依存するため、live E2Eで重複Sessionがないことを確認する。

`clear_session()`はloggerのcurrent Sessionを解除するAPIであり、Galileo上のSessionを終了状態へ変更するremote close APIではない。
したがって、Hermes finalizeとresetはlocal mappingの破棄として定義し、Galileo側のretentionと削除はdata governanceの責務とする。

Session managerは一つのlocal mappingにつき一つのjobを実行し、独自retryを追加しない。
公式SDKまたはHTTP層の内部動作を除き、失敗またはtimeoutしたmappingはfinalize、reset、eviction、process再起動までfailedとして保持する。

会話評価profileを使わない通常profileでは、`HERMES_GALILEO_CAPTURE_CONTENT=false`を維持する。
placeholderだけのSessionに対してConversation Qualityの有用性を保証しない。

## Traceとschemaの要件

| ID | 優先度 | 要件 | 受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| TEL-001 | 必須 | 一回のユーザーターンを一つのtraceとして表す | root spanが`invoke_agent {agent_name}`、kindがローカル呼び出しでは`INTERNAL`、operationが`invoke_agent`になる | 充足 |
| TEL-002 | 必須 | 一つの会話へ複数traceを関連づける | top-level Hermes session IDから作る同じHMAC仮名値をparentとsubagent childの`gen_ai.conversation.id`へ設定し、trace IDや生成UUIDを代用しない | 充足 |
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
| PRI-006 | 必須 | user IDとHermes session IDを既定で仮名化する | raw IDを送らず、productionではversion付きkeyed HMACを使い、同じHermes sessionの相関可能性を保ちながらkeyをtelemetryへ出さない | 一部充足 |
| PRI-007 | 必須 | error情報にも同じ秘匿規則を適用する | content取得無効時はmessageを保存せず、status descriptionには100文字以下のbounded error typeだけを設定し、有効時だけredactor後のexception eventを保存する | 充足 |
| PRI-008 | 必須 | secretを設定とhealthから漏らさない | API keyがspan、log、health snapshot、例外、設定dumpのいずれにも現れない | 一部充足 |
| PRI-009 | 必須 | tenant routingをtrusted configへ限定する | trace processorとSession loggerが同じtrusted projectとlog streamを使い、ユーザー入力で上書きできず、tenant AのSessionまたはtraceがtenant Bへ到達しない | 一部充足 |
| PRI-010 | 必須 | hidden chain-of-thoughtを収集しない | contentと会話履歴の取得設定にかかわらず、既知のhidden reasoning key、Anthropic reasoning block、Gemini `thought_signature`を`[REDACTED REASONING]`へ置換し、reasoning token数と通常fieldへ明示された公開可能なsummaryだけを送る | 充足 |
| PRI-011 | 必須 | data governanceを運用で定める | Galileo側のRBAC、retention、削除、region、API key rotationの責任者と期限が記録される | 外部依存 |

PRI-004はmappingとsequenceの全体をlist化せず、`islice`で設定上限までだけを読み、元collectionの長さから省略数を記録する。
PRI-005の保証対象はGenAI message属性である。
長大なtool argumentsとtool resultはbounded textとして扱い、JSON構造の保持を保証しない。
PRI-006はuser IDとHermes session IDへ同じkey管理方針を適用する。
現行のkeyed HMACには、key versionの記録とrotation期間をまたぐ相関方針が残る。
専用secretもGalileo API keyも渡さずに仮名化関数を使うfallbackでは、低entropy IDに対する辞書攻撃を防げない。
PRI-002はroot inputとLLM API inputの両方へ適用する。
LLM APIでは、履歴取得が無効ならcanonical provider requestの`body.messages`を使わず、hookが別fieldで渡す現在の`user_message`だけを送る。
履歴取得が有効な場合だけ、canonical provider requestの`body.messages`をstructured inputとして送る。
PRI-003はsensitive mapping key、Bearer token、既知API key形式、assignment形式secret、PEM private key、Cookie header、文字列全体または自由text途中のbase64 data URIを置換する。
PRI-009はSDK singletonのAPI key、console URL、API URLを構築前後に検証し、未指定のstale environment値を除去する。
Session loggerにも同じprojectとlog streamを明示して、Session制御面とOTLP data planeのroutingを一致させる。
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
| REL-009 | 必須 | adapterが所有するqueueと相関stateを有界にする | SDK接続前の2048 span buffer、512件のSession解決queue、512件のmapping、512件のsession alias、4096件のpending-ended-spanについてcapacity、現在量、drop数、timeoutとfull時の方針を取得できる | 充足 |
| REL-013 | 必須 | Galileo SDK初期化を遅延実行する | daemon threadでSDKを初期化し、完了spanを最大2048件bufferし、一時失敗を1秒、5秒、30秒、60秒へjitterを加えた間隔で再試行し、replay完了後だけreadyにする | 充足 |

REL-006はSDK接続中のshutdownでflush timeoutまでreadyを待ち、OTLP export HTTP timeoutにも同じ値を渡す。
processor構築前のhealth check、login、current user取得にはgalileo-core側の既定timeoutが使われ、同設定では制御しない。
shutdown後のhookはlockで保護したgateが拒否する。
background flushまたはdelegate cleanupが期限を超える場合はdaemonで直列継続し、`provider_cleanup_deferred`または`delegate_cleanup_deferred`で判別できる。
constructor内のbootstrapまたはstartup replayがdeadlineを超えてconnector daemon上で継続する場合は、runtimeを有界時間で停止状態へ遷移させ、`connector_cleanup_deferred=true`で判別できる。
ただし、background flusherとProvider配下のdeferred processorが別々にflush timeoutを使うため、runtime全体のwall-clockを一つのtimeoutに収める保証はない。
外部注入Providerの`force_flush`がtimeoutを無視する場合もdaemonへ移さない。
`force_flush=True`はqueue drain要求の完了だけを表し、Galileoのdelivery acknowledgmentではない。
REL-009は、2048件のstartup buffer、512件のSession解決queue、512件のmapping、512件のsession alias、4096件のpending-ended-spanと各counterを実装している。
session alias上限ではinactive aliasだけを解放し、すべてactiveなら新しいsubagent観測をfail-openで省略して既存会話の対応を維持する。
`native_session_deferred_span_drops`はSession ID付与待ちを断念した数であり、span自体はSession IDなしで終了する。
REL-013はHTTP 408、429、5xxと、`ImportError`、`TypeError`、`ValueError`以外のstatusなしerrorを再試行する。
その他のstatus付きerror、`ImportError`、`TypeError`、`ValueError`、既存SDK singletonの設定競合では`failed`へ遷移して再試行を止める。
REL-013のretryはSDKのbootstrap初期化だけを対象とし、OTLP batchのdata-plane retryとは区別する。

## Deployment profileの責務

v1の標準構成は**Direct profile**である。
Direct profileのadapterは、Hermes eventの意味変換、export前privacy、Session対応、process内の有界状態、bootstrap接続、local healthを所有する。

OTLP data planeの配送耐久性と集中samplingは、公式SDKまたは別の**Collector profile**が所有する。

| ID | 区分 | 責務または既知保証 | 受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| DIR-001 | Direct profile | 配送保証をlocal acceptanceとbest effort flushへ限定する | `exporter_ready`と`force_flush=True`がGalileo保存ackではなく、process crash後の復元を保証しないことを公開interfaceと運用文書で明示する | 充足 |
| DIR-002 | Direct profile | 採用dependencyのOTLP応答挙動を固定する | connection error、408、5xxのretry、429のno-retry、`Retry-After`非対応、HTTP 200 partial success非検出をwire contract testで検証する | 充足 |
| DIR-003 | Direct profile | dependency内部retryのpayload identityを検証する | 503、408、connection resetのretryでtrace ID、span ID、serialize済みrequest bodyが変わらないことを検証し、ack loss後の重複可能性を文書化する | 一部充足 |
| COL-001 | Collector profile | OTLP data-plane retryとpartial successを扱う | 429で`Retry-After`を尊重し、一時errorを有界に再送し、`partialSuccess.rejectedSpans`をlossとして記録する | 範囲外 |
| COL-002 | Collector profile | crash耐久とqueue self-observabilityを提供する | 暗号化WAL、送信queue、max bytes、max age、queue capacity、oldest age、drop、最終送信成功を運用できる | 範囲外 |
| COL-003 | Collector profile | tail samplingを提供する | error、高latency、高token、canaryを保持し、小率の無作為baselineを残すpolicyをtrace affinity付きで運用する | 範囲外 |

Direct profileで429、`Retry-After`、partial success、WAL、tail samplingをadapterの未充足必須要件とは扱わない。
これらが必要な環境はCollector profileを選択し、同じspan contractとprivacy contractを適用する。
送信先が受理した後にackだけを失う実環境の重複候補は、Galileo read-backで確認する。

## 運用要件

| ID | 優先度 | 要件 | 受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| OPS-001 | 必須 | 設定値をstartup時に検証する | boolean、integer、floatの不正形式と範囲外を具体的な設定名付きで拒否する | 充足 |
| OPS-002 | 必須 | processの状態をsecretなしで取得する | enabled、routing、capture、sample rate、in-flight件数に加え、exporter state、buffer量、drop数、接続試行数、最終接続error type、retry可否、停止理由、connector、Provider、ready delegateのdeferred cleanupを取得し、API keyを含まない | 充足 |
| OPS-003 | 必須 | livenessとdependency状態を分離する | Galileo outageだけではlivenessを失敗させず、queue saturationや受付不能をreadinessまたはstatusで判別できる | 一部充足 |
| OPS-004 | 必須 | adapter所有状態のself-observabilityを提供する | startup connection、startup buffer、Session mapping、queue、pending span、attempt、成功、失敗、timeout、capacity rejection、cancel、deferred cleanupを低cardinalityなhealthで取得できる | 充足 |
| OPS-005 | 必須 | serviceとdeploymentを識別する | resourceにservice name、service version、deployment environment、plugin名が付く | 充足 |
| OPS-006 | 必須 | configuration revisionを識別する | secretを含まない設定revisionとSDK versionをhealthまたはresourceから取得できる | 一部充足 |
| OPS-007 | 必須 | privacy incidentを即時停止できる | operatorが一つの設定で連携を無効化し、既存credentialをrotationし、Galileo上の対象traceを特定できる | 外部依存 |
| OPS-008 | 推奨 | SLOのerror budgetを監視する | delivery、freshness、loss、overhead、privacyのSLIを複数windowのburn rateでalertできる | 未充足 |
| OPS-009 | 必須 | Session対応状態を運用者が確認できる | raw IDやHMAC keyを出さず、pending、ready、failed、capacity drop、timeout、cleanup deferredをhealthから取得し、live read-backでHermes sessionとnative Sessionの対応を検証できる | 一部充足 |

運用手順と初期SLOは、[運用設計](./OPERATIONS.md)に記載する。

## TestとE2Eの要件

| ID | 優先度 | 要件 | 受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| TST-001 | 必須 | Hermes eventからspanへの契約を固定する | canonical event sequenceごとにspan名、kind、parent、status、属性名、型、値を自動contract assertionで比較する | 充足 |
| TST-002 | 必須 | 並行eventを分離する | 複数session、複数turn、同名tool、同一commandとpatternのparallel approval、parallel subagentを交差させてもparentageとIDが混ざらず、alias上限でもactive aliasを解放しない | 充足 |
| TST-003 | 必須 | privacy corpusを検証する | sensitive key、token、cookie、password、private key、data URI、再帰構造、巨大payload、error messageでcanaryが一度もexportされない | 充足 |
| TST-004 | 必須 | Direct profileのOTLP dependency contractを検証する | 401、429、503、408、partial success、timeout、connection reset、large payloadをfake endpointで再現し、採用versionの現行retry回数、loss可視性、retry時のIDとbody保持をassertする | 充足 |
| TST-005 | 必須 | overloadとshutdownを検証する | adapter所有queueのcapacity超過、512 turn超過、TTL、SIGTERM、flush timeout、process強制終了でAgentが停止せず、Direct profileが復元しない境界を確認する | 一部充足 |
| TST-006 | 必須 | live GalileoでSessionとtrace構造を検証する | 専用projectとlog streamへ二ターン以上を送信し、一つのnative Session、Hermes external ID対応、trace、span parentage、model、token、finish reason、tool、errorをAPIまたは画面で確認する | 外部依存 |
| TST-007 | 必須 | Galileo privacyをread-backで検証する | synthetic PIIとcanary secretを送信経路へ投入し、Galileoのspan、metadata、error、logのいずれにも存在しない | 外部依存 |
| TST-008 | 必須 | routing分離を検証する | 誤ったkey、project、log streamを拒否し、二つのtenant fixtureのSessionとtraceが相互のprojectへ現れない | 外部依存 |
| TST-009 | 必須 | SDKとschemaの互換性を検証する | support対象の最小versionと最新versionで同じcontract suiteを実行し、差分をrelease前に承認する | 一部充足 |
| TST-010 | 必須 | GalileoのSession metricを検証する | 会話評価profileでprivacy審査済みの二ターン以上を送り、Galileo log streamで有効化したConversation Qualityが対象native Sessionへ計算される | 外部依存 |
| TST-011 | 必須 | 公式SDKのwire経路を検証する | stub Galileoに対するhealth check、login、current user取得、OTLP protobuf、認証header、routing、parentage、token、secret非混入を一つのE2Eで確認する | 充足 |
| TST-012 | 必須 | native Sessionの制御面とdata planeを結合して検証する | production定数を512件のqueueとmappingおよび4096件のpending spanに固定し、縮小limitのfixtureでcapacity rejection、failed mapping eviction、turn内のSession対応固定、pending-span overflowを再現した上で、SDK stubでSession作成または再利用、HMAC external ID、single-flight、返却UUIDの`galileo.session.id`付与、timeout時fail-openを確認する | 充足 |

TST-001とTST-002の現在状態は、既存の自動検証が示す範囲に限る。
installed distribution metadataのentry pointを実際にloadし、module objectと`register`を検査するunit testも実行する。
TST-003はmapping key、Bearer、generic JWT、既知API key、AWSとGoogleのaccess key、quoted assignment secret、PEM private key、Cookie header、文字列全体と自由text途中のdata URI、hidden reasoning key、Anthropic reasoning block、Gemini `thought_signature`、再帰、文字数上限、巨大sequenceのiteration上限、error messageをunit testで検証する。
PEM private key、Cookie header、自由text途中のdata URI、各形式のhidden reasoning canary非混入はwire E2Eでも検証する。
同一論理API requestの失敗とretry成功を連続させるintegration testは、request IDの維持、1始まりのattempt番号、失敗と成功のstatus、rootの論理request数を検証する。
TST-004は公式SDKからstub endpointまでのwireで401、429、503、408、connection reset、read timeout、partial success、large payloadを再現する。
現行exporterが429をretryせず、partial successをhealthへ反映しない動作もDirect profileのdependency contractとして固定する。
TST-004はadapterへ429 retryまたはpartial success解析を実装する要件ではない。
TST-011は公式SDKからstub endpointまでの通常時wire contractを検証する。
TST-009は日次Workflowで最新Galileoを検証するが、support下限versionを明示installする別jobがないため一部充足である。
実Galileoでのread-back、表示、Session再利用、評価はTST-006からTST-010の外部依存として残る。

## GitHub Actionsの要件

| ID | 優先度 | 要件 | 受入基準 | 現在状態 |
| --- | --- | --- | --- | --- |
| CI-001 | 必須 | pull requestでlocal contractを検証する | support対象Python matrixでlint、format check、unit、integration、stub wire E2Eを実行し、baselineの実Hermes sourceでPluginManager registrationと全observer hookを検証し、required checkが失敗したpull requestをmergeしない | 一部充足 |
| CI-002 | 必須 | HermesとGalileoの更新を日次確認する | `schedule`と手動実行でHermes mainのcommit SHA、PyPI project metadataが示すGalileo current release、およびpipが解決したGalileoの全依存closureをbaselineと比較し、更新時だけ検出したexact closureで全contract testを実行する | 充足 |
| CI-003 | 必須 | dependency更新結果を追跡可能にする | 最新版test成功時は検証済みbaseline更新をpull requestとして提示し、失敗時は対象SHAとversionをjob summaryへ、contract failureをstep logへ残す | 充足 |
| CI-004 | 必須 | secretを使うlive Galileo E2Eを自動化する | trusted pull request、`main`、`test.yml`の手動run、更新を検出したdaily run、`force_test=true`のdependency watchではrepository secretの`GALILEO_API_KEY`を必須にし、事前作成した専用projectで公式SDKが専用log streamを作成または再利用してConversation Qualityを有効化し、ingestion、native Session、複数turn、privacy canary、Conversation Qualityをread-backし、secret欠落時はfail-closedにする | 外部依存 |
| CI-005 | 必須 | pull requestへsecretを安全に渡す | 同一repositoryの信頼済みpull requestだけをlive E2E対象とし、fork pull requestではskipし、`pull_request_target`を使わず、job permissionsを最小化する | 充足 |
| CI-006 | 必須 | Workflow自体の供給網と実行量を制御する | third-party actionをcommit SHAでpinし、timeoutと最小権限を設定し、pull request runは同じrefの古いrunをcancelし、dependency watchはbaseline更新競合を避けるため直列実行する | 充足 |

CI-004のGalileo project、log stream、API key、metric実行基盤、read APIはrepository外の運用資源である。
そのため、Workflow実装の充足状態とlive Galileo受入の外部依存を分けて評価する。
CI-001のWorkflowは実装済みだが、required checkとmerge protectionの有効化はGitHub repository設定に依存する。
実Hermes互換checkの範囲はsource上のPluginManager、entry point、registration、全observer hookの呼び出しまでであり、Hermes本体のpackage install、実LLMを使うAgent loop、gateway起動は含まない。
fork pull requestとDependabotだけはsecretを渡さずlive E2Eをsafe skipし、secret不要のlocal checksを必須にする。
fork pull requestはlive受入を完了したことにならないため、merge前に同じcommitをtrusted branchで再検証する。
更新がないdaily runはversion比較だけで完了し、live E2Eとsecret確認を行わない。
live E2EがConversation Qualityを有効化したCI log streamでは、metric assertionも必須にする。

## 初期受入ゲート

本番リリース候補は、次の条件をすべて満たす必要がある。

1. 必須要件の「未充足」が解消されるか、期限、owner、軽減策を持つ例外として承認されている。
2. TST-003のcanary secretが全経路で0件である。
3. TST-006で一つのnative Sessionに二つ以上のHermes turn traceが表示され、HMAC external ID、`galileo.session.id`、parentage、必須属性が一致する。
4. TST-004でpartial success、429、401、connection resetに対する採用dependencyの実挙動が固定されている。
5. Agent経路の同期overheadが[運用設計](./OPERATIONS.md)のSLO候補を満たす。
6. SDKとGenAI Semantic Conventionsのversionがrelease artifactへ記録されている。
7. CI-001のpull request checksとCI-002のdaily dependency watchが有効である。
8. 会話評価profileを提供するreleaseでは、secretを使うlive E2EでConversation Qualityが対象native Sessionへ計算される。

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
- [Galileo Logger](https://docs.galileo.ai/sdk-api/logging/galileo-logger)
- [Galileo Session](https://docs.galileo.ai/concepts/logging/sessions/using-sessions)
- [Galileo Conversation Quality](https://docs.galileo.ai/concepts/metrics/agentic/conversation-quality)
- [Galileo OpenTelemetry統合概要](https://docs.galileo.ai/sdk-api/third-party-integrations/opentelemetry-and-openinference)
- [Galileo OpenTelemetry推奨事項](https://docs.galileo.ai/sdk-api/third-party-integrations/opentelemetry-and-openinference/integration-recommendations)

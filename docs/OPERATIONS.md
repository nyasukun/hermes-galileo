# Hermes Galileo連携の運用設計

- 運用基準日：2026-07-24
- 対象：direct Galileo SDK構成
- 関連文書：[要件定義](./REQUIREMENTS.md)、[設計](./DESIGN.md)

## v1運用モデル

v1のDirect profileは、Hermes process内でOpenTelemetry spanを生成し、deferred processorを介して公式`GalileoSpanProcessor`からGalileoへ直接送信する。
deferred processorは公式SDKのhealth check、login、current user取得をdaemon threadで実行し、接続前とstartup replay中に終了したspanを最大2048件保持する。
startup replay完了後だけreadyになり、恒久errorではfailedへ遷移してretryを止める。

Session制御面では、固定数のdaemon workerが公式`GalileoLogger.start_session(external_id=...)`を呼ぶ。
Hermes session IDはHMACで仮名化し、同じ仮名値をGalileo external ID、`gen_ai.conversation.id`、`hermes.session.id`へ使う。
subagent childはtop-level parentの会話HMACとnative Sessionを共有し、child自身のHMACはsubagent相関属性だけに使う。
公式APIが返すUUIDを`galileo.session.id`として関連spanへ設定し、一つのHermes会話に含まれる複数turn traceを同じGalileo native Sessionへ入れる。

障害時はAgentを優先する。
telemetryの初期化、Session解決、span生成、flush、exportが失敗しても、Hermesのユーザー応答を失敗させない。

Direct profileは、local acceptanceとbest effort flushを提供する。
ready後のOTLP data-plane retry、partial success解析、永続WAL、tail samplingはDirect adapterの責務に含めない。

## 設定

設定面は二つに分ける。
credentialとGalileo routingはactive profileの`$HERMES_HOME/.env`へ置き、telemetry shapingは`$HERMES_HOME/plugins/hermes_galileo/config.yaml`へ置く。
`HERMES_HOME`未指定時は`~/.hermes`を使う。
各fieldは、`HERMES_GALILEO_*`環境変数、`config.yaml`、組み込み既定値の順で解決する。
空の環境変数は未指定として扱い、同じfieldのYAML値を消さない。
環境変数名から`HERMES_GALILEO_`を除いて小文字にした名前がYAML field名である。
たとえば、`HERMES_GALILEO_SAMPLE_RATE`は`sample_rate`に対応する。

v1は初期化時のactive Hermes homeへGalileo routingをbindingする。
Hermesのmultiplexed gatewayが別profile contextのhookを同じprocessへ流した場合は、そのeventをdropして`profile_scope_mismatches`を増やす。
複数profileを観測する場合はprofileごとにHermes processを分ける。
Galileo SDKのprocess-global singletonへprofile別credentialやprojectを同居させない。

`config.yaml`は任意であり、次のtemplateから作成する。

```bash
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
cp "$hermes_home/plugins/hermes_galileo/config.yaml.example" \
  "$hermes_home/plugins/hermes_galileo/config.yaml"
```

YAML parserにはruntime dependencyとしてPyYAMLを使う。
YAMLのrootはmappingでなければならず、boolean、integer、number、stringをfield定義どおりに記述する。
構文error、wrong root、未知field、型違反、範囲違反は`ConfigurationError`としてstartup時に拒否する。
標準pathのファイルが存在しない場合だけは、環境変数と組み込み既定値へfallbackする。
設定errorはadapterを無効化するが、Hermesの起動と応答を失敗させない。

### Galileo公式設定

| 環境変数 | 必須条件 | 既定値 | 機微性 | 動作 |
| --- | --- | --- | --- | --- |
| `GALILEO_API_KEY` | 連携有効時 | なし | secret | Galileo認証 |
| `GALILEO_PROJECT` | 連携有効時 | なし | internal | 送信先project |
| `GALILEO_LOG_STREAM` | 連携有効時 | なし | internal | 送信先log stream |
| `GALILEO_CONSOLE_URL` | 自社運用版 | Galileo Cloud | internal | 公式SDKのconsole endpoint |
| `GALILEO_API_URL` | `GALILEO_CONSOLE_URL`設定時 | Galileo公式default | internal | Galileo API endpointの明示pin |
| `GALILEO_LOGGING_DISABLED` | 任意 | `false` | public | `true`でSDK連携を無効化 |

`GALILEO_API_KEY`はsecret managerからprocess environmentへ注入する。
設定ファイル、command line、trace attribute、health responseへ書かない。
API keyとpseudonym secretは`Settings`の`repr`から除外されるが、設定object自体をapplication logへ出さない。
custom `GALILEO_CONSOLE_URL`を設定する場合は、既存singletonの有無にかかわらず`GALILEO_API_URL`も必ず明示する。
設定でconsole URLまたはAPI URLを省略すると、runtime初期化はprocess environmentに残った同名のstale値を除去する。
この表のfieldは`config.yaml`で受け付けない。
YAMLへ書いた場合は設定errorとしてadapterを無効化する。

### hermes-galileo設定

| YAML field | 環境変数 | 既定値 | 許容範囲 | 動作 |
| --- | --- | --- | --- | --- |
| `enabled` | `HERMES_GALILEO_ENABLED` | `true` | boolean | 連携全体の有効化 |
| `capture_content` | `HERMES_GALILEO_CAPTURE_CONTENT` | `false` | boolean | 秘匿済みcontentの取得 |
| `capture_conversation_history` | `HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY` | `false` | boolean | root inputで会話履歴を優先 |
| `hash_user_ids` | `HERMES_GALILEO_HASH_USER_IDS` | `true` | boolean | user IDを仮名化 |
| 不可 | `HERMES_GALILEO_PSEUDONYM_SECRET` | 空 | secret文字列 | user IDとHermes session IDのHMAC key |
| `max_content_chars` | `HERMES_GALILEO_MAX_CONTENT_CHARS` | `12000` | 256から1000000 | 一つのcontent文字数上限 |
| `max_collection_items` | `HERMES_GALILEO_MAX_COLLECTION_ITEMS` | `100` | 1から10000 | mappingまたはsequenceの要素上限 |
| `sample_rate` | `HERMES_GALILEO_SAMPLE_RATE` | `1.0` | 0.0から1.0 | root traceのhead sampling率 |
| `turn_ttl_seconds` | `HERMES_GALILEO_TURN_TTL_SECONDS` | `900` | 30から86400 | inactive turn stateのTTL |
| `async_flush_on_turn_end` | `HERMES_GALILEO_ASYNC_FLUSH_ON_TURN_END` | `true` | boolean | turn終了時のbackground flush |
| `flush_timeout_millis` | `HERMES_GALILEO_FLUSH_TIMEOUT_MILLIS` | `10000` | 100から120000 | force flush、OTLP export、各cleanup待ちのtimeout |
| `native_sessions_enabled` | `HERMES_GALILEO_NATIVE_SESSIONS_ENABLED` | `true` | boolean | Galileo native Sessionの作成または再利用 |
| `native_session_timeout_millis` | `HERMES_GALILEO_NATIVE_SESSION_TIMEOUT_MILLIS` | `5000` | 100から120000 | local Session解決期限とpending span保持時間 |
| `debug` | `HERMES_GALILEO_DEBUG` | `false` | boolean | plugin loggerのDEBUG出力とSession API失敗時traceback |
| `environment` | `HERMES_GALILEO_ENVIRONMENT` | `development` | string | deployment environment属性 |
| `service_name` | `HERMES_GALILEO_SERVICE_NAME` | `hermes-agent` | string | OpenTelemetry service name |

booleanは、`1`、`true`、`yes`、`on`と、`0`、`false`、`no`、`off`を大文字小文字を区別せず受け入れる。
それ以外の値と数値範囲外はstartup時に拒否する。

`HERMES_GALILEO_CAPTURE_CONTENT=true`は、無加工のfull captureを意味しない。
既知secretの秘匿、binary省略、collection上限、文字数上限を適用したcontentだけを送る。

`HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY`は、content取得が無効なら実内容を送らない。
content取得が有効でも履歴取得が無効なら、LLM API spanはcanonical provider requestの蓄積済み`body.messages`ではなく、別fieldの現在の`user_message`だけを送る。
履歴取得が有効な場合だけ、`body.messages`をstructured inputとして送る。

`HERMES_GALILEO_PSEUDONYM_SECRET`を設定すると、user IDとHermes session IDはHMAC-SHA-256で仮名化される。
未設定時はGalileo API keyをHMAC keyとして使うため、productionでは相関IDのrotationを認証keyのrotationから分離する専用secretを設定する。

`HERMES_GALILEO_NATIVE_SESSIONS_ENABLED=false`は、SDK互換性障害時にtrace exportだけを継続するための緊急degradation設定である。
native SessionとConversation Qualityを必須とするv1受入では`true`を維持する。

`HERMES_GALILEO_DEBUG=true`はplugin loggerをDEBUG levelにし、native Session API失敗時のtracebackを有効にする。
debug目的でもHermes event payloadとcredentialを意図的にlogへ出さない。

### Content profile

通常profileは、次の設定を維持する。

```dotenv
HERMES_GALILEO_CAPTURE_CONTENT=false
HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY=false
```

通常profileでもnative Sessionと複数turnの対応は行うが、trace inputとoutputはprivacy placeholderである。
placeholderだけのSessionに対してConversation Qualityの有用性を前提にしない。

Conversation Qualityを使う会話評価profileは、次の条件をすべて満たす。

- `HERMES_GALILEO_CAPTURE_CONTENT=true`を明示する。
- 過去履歴の重複送信が必要と確認できない限り、`HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY=false`を維持する。
- 対象dataについてdata ownerとsecurity reviewerの承認を得る。
- Galileo log stream側でConversation Qualityを有効化し、metric計算に使うjudge設定とcost ownerを記録する。
- Sessionに含まれる全turnを評価する環境では`HERMES_GALILEO_SAMPLE_RATE=1.0`にする。
- redaction、hidden reasoning除去、payload上限を無効化しない。

content captureはadapterの設定であり、Conversation Qualityの有効化と計算はGalileo log stream側の設定である。
この二つを別の変更管理対象として記録する。

## 配備前checklist

### Security

- Galileo API keyを対象projectとlog streamに必要な最小権限で発行する。
- keyのowner、rotation期限、失効手順を記録する。
- pseudonym secretをAPI keyと分け、key versionとrotation時の相関期間を記録する。
- projectとlog streamをenvironmentごとに分ける。
- productionでcontent取得を有効にする場合は、data ownerとsecurity reviewerの承認を得る。
- GalileoのRBAC、retention、削除、data regionを確認する。
- Session名、Session metadata、task ID、tool名に個人情報を含めない。
- raw Hermes session IDがexternal ID、span、log、healthへ出ないことを確認する。

### Compatibility

- installed distributionの`hermes_agent.plugins` entry pointが、callableな`register`を持つmodule objectを返すtestを実行する。
- Galileo SDK、OpenTelemetry SDK、GenAI Semantic Conventionsのversionを記録する。
- support対象の最小SDK versionと採用versionでcontract testを実行する。
- 採用したGalileo SDKで`GalileoLogger.start_session(external_id=...)`の作成と既存Session再利用をcontract testする。
- Session loggerとspan processorが同じprojectとlog streamを使うことを確認する。
- Galileoのminimum valid spanに、placeholder状態のinputとoutputが受理されることを確認する。
- project名とlog stream名の大文字小文字、空白、存在を確認する。
- custom deploymentではconsole URLとAPI URLを別々に確認する。
- 同じprocessで他のGalileo instrumentationが先にSDK singletonを作る場合は、API key、console URL、API URLを一致させる。

### Capacity

- peak turn数、turn内API call数、tool call数、平均payload sizeを見積もる。
- `sample_rate`とcontent上限を決める。
- 512件のin-flight turn上限へ到達しないことをload testで確認する。
- SDK接続前とstartup replay中に終了するspanが2048件のstartup bufferへ到達しないことをload testで確認する。
- startup bufferは満杯時に最古spanを捨てるため、`dropped_spans`の増加を失敗条件にする。
- 固定worker数2、Session解決queue 512件、local mapping 512件、pending-span buffer 4096件が上限へ到達しないことを複数sessionと複数turnのload testで確認する。
- session alias 512件の上限ではactive aliasを解放せず、新しいsubagent観測だけを省略してcounterを増やすことを確認する。
- Session解決timeoutとcapacity dropを別々に観測できることを確認する。
- 接続後のSDK processor queue capacityとfull時の挙動はhealthから見えないため、採用versionの負荷試験とlive read-backで別に確認する。
- shutdown grace periodをflush timeoutより長くする。

### Subagent event順序

canonicalなevent順序は、`subagent_start`の後にchild sessionとchild turnのeventが続く形である。
順序が逆転しても、まだexportされていないactive spanとSession解決待ちの終了spanは、後着した`subagent_start`でparentのnative Sessionへ再束縛される。
すでにexport済みのchild spanは再束縛できない。
child用`start_session`のnetwork requestがすでに始まった場合もremote Sessionを取り消せず、遅れて返った結果はlocalで無視される。
session aliasは最大512件とし、上限ではinactive aliasだけを解放する。
すべてactiveなら新しいsubagent観測を省略して`session_alias_capacity_rejections`を増やし、既存aliasは維持する。
順序逆転を検出した場合は、child用の重複native Sessionと、親Session IDなしで先行exportされたspanの有無をlive read-backで確認する。

## Health、readiness、status

現行runtimeはHTTP endpointを直接提供せず、programmatic health snapshotを提供する。
host processがKubernetesなどで動作する場合は、このsnapshotとexporter metricsからprobeを構成する。

[Kubernetes Probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/)に従い、livenessとreadinessを分ける。

### Liveness

livenessは、processとevent loopまたはworker threadが動作しているかだけを判定する。
Galileo outage、429、認証errorを理由にlivenessを失敗させない。
外部依存障害で再起動を繰り返すと、queueと未送信spanを失う可能性が増えるためである。
Session APIのtimeoutまたは失敗もlivenessを失敗させない。

### Readiness

readinessは、新しい観測eventを安全に受け入れられるかを判定する。
現行runtimeはreadiness判定そのものを返さないため、host processがhealth snapshotから次の条件を評価する。

- 設定のsyntaxと必須値が検証済みである。
- runtimeとProviderが初期化済みである。
- shutdown処理へ入っていない。
- in-flight turnが512件の上限へ接近していない。
- `exporter_state`が`connecting`または`replaying`の場合は、`buffered_spans`が2048件の上限へ接近しておらず、`dropped_spans`が増加していない。
- Session解決queueとpending-span bufferがcapacityへ接近しておらず、Session timeoutとcapacity dropが継続的に増えていない。

Galileo SDKを初期化中またはstartup bufferをreplay中でも、bufferに十分な余地があれば`degraded`としてreadyを維持できる。
bufferが飽和し、継続的なlossが避けられない場合はnot readyにする。
`exporter_state=failed`は自動回復しないためnot readyとし、設定またはcredentialを修復してprocessを再起動する。
Session APIを解決中でもbufferに余地があればdegradedとしてreadyを維持する。
Session timeoutまたはcapacity dropが継続し、native Session完全性を維持できない場合はobservability readinessを失敗させる。

`exporter_ready=true`は公式processorのconstructorとstartup replayの完了を示すが、Galileoへのbatch受理を示さない。
この値はnative Session解決の成功も示さない。
現行health snapshotでは接続後のSDK queue、最終export成功、Galileoへのdeliveryを取得できないため、end-to-endのreadiness判定は未実装である。

### Status

現行health snapshotは、次の値をsecretなしで返す。

- `enabled`
- `project`と`log_stream`
- `capture_content`と`sample_rate`
- `inflight_turns`、`inflight_child_spans`、`inflight_subagents`
- `turns_started`、`turns_finished`、`spans_started`、`spans_finished`
- `orphaned_spans`と`initialization_errors`
- `exporter_ready`
- `exporter_state`
- `buffered_spans`
- `dropped_spans`
- `connection_attempts`
- `last_connection_error_type`
- `last_connection_error_retryable`
- `retry_stopped_reason`
- `connector_cleanup_deferred`
- `provider_cleanup_deferred`
- `delegate_cleanup_deferred`
- `native_sessions_enabled`
- `session_aliases`
- `pending_session_releases`
- `native_session_state`
- `native_session_pending`、`native_session_ready`、`native_session_failed`
- `native_session_mappings`、`native_session_queue_depth`
- `native_session_callbacks_inflight`
- `native_session_attempts`、`native_session_resolved`、`native_session_failures`
- `native_session_timeouts`、`native_session_capacity_rejections`、`native_session_mapping_evictions`
- `native_session_cancelled`、`native_session_release_pending`
- `native_session_deferred_spans`、`native_session_deferred_span_drops`
- `native_session_worker_calls_inflight`、`native_session_worker_cleanup_deferred`

`last_connection_error_type`はclass名だけであり、error messageやcredentialを含めない。
`last_connection_error_retryable`は真偽値または未設定であり、`retry_stopped_reason`は低cardinalityの停止理由である。
`dropped_spans`はstartup bufferのoverflow、replay失敗、恒久初期化失敗、failed、stopping、stopped状態で終了したspan、shutdown期限切れを数える。
ready後のenqueue失敗、接続後のSDK queue、exportedまたはrejected span数、OTLP retry数、最終export成功時刻、export latency、partial successは含まれない。
Session healthは件数と状態だけを返し、raw Hermes session ID、HMAC値、Galileo Session UUID、pseudonym secretを含めない。
`native_session_deferred_span_drops`は4096件のpending-span bufferへ保持できず、Session IDなしで直ちに終了したspan数であり、span自体の破棄数ではない。
`provider_cleanup_deferred`は、background flusherがruntime deadlineを超え、Provider cleanupがdaemonへ延期されたことを示す。
`delegate_cleanup_deferred`はdelegate cleanup threadの実行中を示し、shutdown返却後も`true`ならdeferred processorのdeadlineを超えて継続している。
`connector_cleanup_deferred`は、runtimeが`stopped`でもconnector threadがconstructor、startup replay、またはその後のcleanup内で継続していることを示し、thread終了後に`false`へ戻る。

## 初期SLIとSLO

次の数値は、実trafficのbaselineがない段階の開始目標である。
30日間の測定後に、ユーザー影響とcostを基に見直す。

[Google SREのService Level Objectives](https://sre.google/sre-book/service-level-objectives/)に従い、平均値ではなくpercentile、成功率、error budgetを使う。

| SLI | 初期SLO | 測定方法 | 現行の測定可否 |
| --- | --- | --- | --- |
| Agent同期overhead | p99が5ミリ秒以下 | 同一fixtureのhook有無によるturn hot-path差分 | 自動benchmarkが必要 |
| Agent isolation | telemetry原因のuser-visible failureが0件 | hook例外注入とAgent結果比較 | 測定可能 |
| Local acceptance | valid spanの99.9%以上をprocessorへ受理 | accepted ÷ generated | startup中だけbufferとdropから一部測定可能 |
| Native Session attachment | eligible turn traceの99.9%以上が期待する`galileo.session.id`を持つ | Session付きtrace ÷ sampled eligible trace | local healthとlive read-backが必要 |
| Multi-turn completeness | 完了したHermes会話の99%以上で期待turn数とnative Session内trace数が一致 | Session内trace数 ÷ 完了turn数 | live read-backが必要 |
| Delivery freshness | accepted spanの99.5%以上が5分以内にGalileoへ到達 | end timeからGalileo read-back time | live計測が必要 |
| Trace completeness | sampled traceの99%以上が必須spanと属性を持つ | contract validator | 測定可能 |
| Telemetry loss | queue-full、non-retryable、partial rejectionの合計が0.1%以下 | lost ÷ generated | startup dropだけ測定可能 |
| Duplicate | 重複span IDが0.1%未満 | 同一trace IDとspan IDの重複集計 | Galileo read-backが必要 |
| Privacy canary | known secretとsynthetic PIIの漏えい0件 | export前captureとGalileo read-back検索 | live計測が必要 |
| Routing isolation | 別tenantへの誤配送0件 | projectとlog stream別canary | live計測が必要 |
| Conversation Quality coverage | 会話評価profileのeligible Sessionの99%以上でmetricが期限内に計算 | metric付きSession ÷ eligible Session | Galileo log stream設定とlive計測が必要 |

privacyとtenant誤配送は通常のerror budgetへ含めない。
一件でも確認した場合はsecurity incidentとして扱う。

Agent品質は配送SLOと分離する。
Action Completion、Tool Error、Agent Efficiency、Agent Flow、Conversation Quality、安全性をagent、prompt、model version別に追跡する。
Galileoのmetric定義は、[Agentic Metrics Overview](https://docs.galileo.ai/concepts/metrics/agentic/agentic-overview)を基準にする。
Conversation Qualityの計算可否は、native Sessionの完全性、trace inputとoutput、Galileo log streamのmetric設定に依存する。

## Dashboardとalert

### Dashboard

現行health snapshotから、最低限次の時系列を作る。

- `exporter_ready`と`exporter_state`
- `buffered_spans`と固定上限2048に対するstartup buffer使用率
- `dropped_spans`の増分
- `connection_attempts`の増分、`last_connection_error_type`、`last_connection_error_retryable`、`retry_stopped_reason`
- `connector_cleanup_deferred`、`provider_cleanup_deferred`、`delegate_cleanup_deferred`
- in-flight turn、child span、subagent、orphaned span
- `native_session_pending`、`native_session_ready`、`native_session_failed`
- `native_session_queue_depth`、`native_session_mappings`、`native_session_deferred_spans`
- `native_session_capacity_rejections`、`native_session_mapping_evictions`、`native_session_timeouts`、`native_session_deferred_span_drops`
- `session_alias_capacity_rejections`
- `profile_scope_enforced`、`profile_scope_mismatches`
- `native_session_worker_cleanup_deferred`

Galileo read-backまたはCollector profileを追加した段階で、次の時系列を同じdashboardへ加える。

- generated、accepted、exported、rejected、dropped span rate
- 接続後のqueue utilizationとoldest age
- export latency p50、p95、p99
- OTLP retry rateと最終failure rate
- HTTPまたはgRPC response code
- TTL timeoutとstate eviction
- content capture率と平均payload size
- model別input、output、cache、reasoning token
- model別およびtool別error rate
- agent version、prompt version、model version別の品質metric
- native Session当たりのturn trace数とSession metric coverage

session ID、user ID、trace IDをmetric labelへ使わない。
個別調査の相関にはtrace検索を使う。

### Alert

単一eventではなく、複数windowのerror-budget burn rateを使う。
現行health snapshotだけで構成できる初期alert条件は次のとおりである。

- `exporter_state=connecting`または`replaying`が5分継続し、その間にspanが終了している。
- `exporter_state=failed`へ遷移する。
- startup buffer使用率が80%を15分継続する。
- `dropped_spans`が1件以上増加する。
- `connection_attempts`が増え続け、`last_connection_error_type`が空でない。
- `native_session_failures`、`native_session_capacity_rejections`、`native_session_timeouts`、`native_session_deferred_span_drops`のいずれかが1件以上増加する。
- `native_session_deferred_spans`が固定上限4096の80%を15分継続する。
- shutdown後もdeferred cleanup flagが運用で定めた猶予を超えて継続する。

最終export成功からの経過時間、partial rejection、HTTP 401、403、429、non-retryable schema errorは、現行healthだけではalertにできない。
これらはCollector profileまたはGalileo read-backで観測経路を追加した後に有効化する。

state evictionはtraceまたはlogから検出し、1件以上でalertにする。
privacy canaryとrouting canaryはGalileo read-backで検出し、1件以上でalertにする。
privacy canaryとrouting canaryは即時通知する。
会話評価profileでは、eligible native SessionにConversation Qualityが期限内に計算されない状態をalertにする。
その他はtraffic量とerror budgetを併用し、idle環境の誤alertを避ける。

## Sampling運用

developmentとstagingは、原則`sample_rate=1.0`とする。
productionは、trafficとGalileo costを測定してratioを決める。

head samplingを下げる場合も、`ParentBased`によって一つのtrace全体を同じdecisionにする。
sampling判断に使うagent、provider、modelはspan開始時に設定する。

error、高latency、高token、canaryを事後条件で必ず保持する必要が生じた場合は、direct SDKだけで解決しない。
[OpenTelemetry Tail Sampling Processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/README.md)を使うCollector profileへ移行する。
その場合も、小率の無作為baselineを残して正常系の偏りを避ける。

trace samplingとGalileoの評価metric samplingは別々に管理する。
前者はspanを送るかを決め、後者は到着したtraceへ評価を実行するかを決める。
Conversation Qualityで会話全体を評価するprofileは、Session内turnの欠落を避けるため`sample_rate=1.0`を使う。

## Retryとbackpressure

### Native Session解決

hook callbackはSession APIを同期実行せず、最大512件のqueueへ解決要求を登録する。
runtimeはraw Hermes session IDをHMAC external IDへ変換してからqueueへ登録する。
二つのdaemon workerは同じHMAC external IDの要求をsingle-flightにまとめ、その仮名値だけで公式`start_session`を呼ぶ。
返却UUIDを得るまでに終了したspanは、有界なpending-span bufferへ保持する。
local Session mappingは最大512件、pending-span bufferは最大4096件である。
mapping上限では最終利用が最も古いfailed mappingだけを解放して`native_session_mapping_evictions`を増やす。
pendingまたはready mappingしかない場合は既存会話の対応を維持し、新しい会話だけをSessionなしでfail-openして`native_session_capacity_rejections`を増やす。
`native_session_timeout_millis`はlocal mappingとpending spanをfail-open解放するdeadlineであり、実行中の同期`start_session()`をcancelまたはHTTP timeout設定しない。
二つのworkerがSDK call内で停止すると後続要求も期限切れになり得るため、`native_session_worker_calls_inflight`、queue depth、timeout増分を同時に監視する。

解決成功時は`galileo.session.id`を付けてpending spanをreplayする。
timeout、Session API失敗、capacity超過ではSession IDなしでreplayし、Hermesとtrace生成を継続する。
同じexternal IDの後続要求は公式SDKの既存Session再利用を使う。

Session workerは一つのlocal mappingにつき公式`start_session`を一回呼び、独自retryを行わない。
失敗またはtimeoutしたmappingはfinalize、reset、eviction、process再起動までfailedとして保持する。
finalizeまたはreset時に解決中のmappingは`native_session_release_pending`として残り、成功、timeout、cancel callbackでpending spanを解放した直後に削除される。
top-level parentのturn終了、finalize、reset時に実行中またはqueue待ちのsubagent delegationが残る場合は、`subagent_stop`までmappingとaliasを維持し、`pending_session_releases`へ記録する。
timeoutまたはcancel callbackは同じSession解決generationへ束縛されたspanだけをfail-open解放し、同じexternal IDで再開した新turnへ影響させない。
解決前に同じexternal IDの新lifecycleを開始した場合はlocal release予約を取り消す。
一つのturn内のLLM、tool、approval、subagent spanはrootのgenerationを継承し、mapping回収後もturn途中で再解決しない。
mapping再作成後のSession再解決は、ready後のOTLP batchを再送するdata-plane retryではない。
`native_session_pending`、`native_session_ready`、`native_session_failed`、`native_session_capacity_rejections`、`native_session_mapping_evictions`、`native_session_timeouts`、`native_session_worker_calls_inflight`、`native_session_worker_cleanup_deferred`を別々に監視する。

### SDK初期化

公式`GalileoSpanProcessor`のconstructorはhealth check、API key login、current user取得を行う。
deferred processorはこのconstructorをdaemon threadで実行する。
HTTP 408、429、500以上と、`ImportError`、`TypeError`、`ValueError`以外のstatusなしerrorでは、1秒、5秒、30秒、60秒へ0.8倍から1.2倍のjitterを加えて再試行する。
5回目以降も60秒を基準にし、接続成功またはshutdownまで続ける。
startup retryはHTTP 429を対象にするが、`Retry-After`は利用しない。
HTTP 408、429、500以上以外のstatus付きerror、`ImportError`、`TypeError`、`ValueError`、既存Galileo SDK singletonの設定競合では`failed`へ遷移し、再試行を止める。

SDKがreadyになる前に終了したspanは、memory上のstartup bufferへ最大2048件保持する。
bufferが満杯なら最古spanを捨て、`dropped_spans`を増やす。
constructor完了後は保持したspanを公式processorへ順番にreplayし、replay完了後だけ`ready`へ遷移する。
replay時にdelegateの`on_end`が失敗した場合はwarningを記録し、`dropped_spans`へ加算する。
恒久初期化失敗では保持中と以後のspanを`dropped_spans`へ加算する。

### Direct profileのOTLP batch送信

現行依存関係で確認したOpenTelemetry Python 1.44.0のOTLP HTTP exporterの挙動は次のとおりである。

| 状態 | Direct profileの既知動作 | 運用上の意味 |
| --- | --- | --- |
| connection error | exporter内部で再試行する | retry回数と最終dropをhealthから取得できない |
| HTTP 408 | exporter内部で再試行する | retry回数をhealthから取得できない |
| HTTP 500から599 | exporter内部で再試行する | retry回数をhealthから取得できない |
| HTTP 429 | 再試行しない | `Retry-After`を使う配送SLOはDirect profileで提供しない |
| HTTP 2xx | response bodyを解析せず成功を返す | `partialSuccess`のrejected数はDirect adapterから観測できない |
| その他のHTTP 4xx | 再試行しない | response code別counterとalertがない |

この挙動は、[OpenTelemetry Python 1.44.0 OTLP HTTP trace exporter](https://github.com/open-telemetry/opentelemetry-python/blob/v1.44.0/exporter/opentelemetry-exporter-otlp-proto-http/src/opentelemetry/exporter/otlp/proto/http/trace_exporter/__init__.py)と[同versionのHTTP retry判定](https://github.com/open-telemetry/opentelemetry-python/blob/v1.44.0/exporter/opentelemetry-exporter-otlp-proto-http/src/opentelemetry/exporter/otlp/proto/http/_common/__init__.py)で確認した。
現在のdependency constraintは`galileo[otel]>=2.5.1,<3`であり、OpenTelemetry 1.44.0を直接pinしていないため、依存更新時は同じwire contractを再確認する。

この表は採用dependencyのcontractであり、Direct adapterの未充足必須要件ではない。
429で`Retry-After`を尊重すること、partial successを解析すること、retry回数と最終dropを計測することが必要な環境はCollector profileを選ぶ。

startup bufferはboundedであり、queue full時にAgentをblockしない。
一方、接続後の公式SDK内部queue capacityとdropは現行healthから観測できない。
法令または監査要件でlossを許容できない場合は、暗号化した永続spoolまたはmessage queueを使うCollector profileへ移行する。

## 障害対応

### Startupで必須設定が欠ける

1. errorに列挙された環境変数名を確認する。
2. secret managerの注入先processと実行userを確認する。
3. API keyの値をlogまたはticketへ貼らない。
4. Agent継続を優先する場合は連携を無効化し、設定修復後に再起動する。

### TraceがGalileoへ現れない

1. runtimeが有効か、sample rateが0でないかを確認する。
2. projectとlog streamが対象environment用かを確認する。
3. custom deploymentでは`GALILEO_CONSOLE_URL`と`GALILEO_API_URL`が意図したendpointかを確認する。
4. `exporter_state`、`connection_attempts`、`last_connection_error_type`、`last_connection_error_retryable`、`retry_stopped_reason`を確認する。
5. `buffered_spans`と`dropped_spans`を確認する。
6. `spans_started`と`spans_finished`の差を確認する。
7. `exporter_ready=true`でもdelivery成功を意味しないため、SDK logとGalileo read-backを確認する。
8. 専用canary turnを一つ実行し、trace IDで検索する。
9. raw promptやAPI keyをdiagnostic logへ追加しない。

### Traceがnative Sessionへ入らない

1. `native_session_pending`、`native_session_ready`、`native_session_failed`、`native_session_capacity_rejections`、`native_session_mapping_evictions`、`native_session_timeouts`を確認する。
2. Session loggerとspan processorのprojectおよびlog streamが一致することを確認する。
3. raw Hermes session IDではなく、`hermes:`接頭辞付きHMAC external IDでSessionを検索する。
4. 同じHermes sessionの複数turnで`gen_ai.conversation.id`が一致することを確認する。
5. 各spanの`galileo.session.id`が、external IDに対応するnative Session UUIDと一致することを確認する。
6. finalizeまたはreset時に解決中なら`native_session_release_pending`となり、成功、timeout、cancel後に古いmappingが削除され、同じexternal IDの再開時はrelease予約が取り消されることを確認する。
7. 複数processが同じexternal IDを同時に初回作成した場合は、重複native Sessionを検索する。
8. Session API障害時はtraceがSession IDなしでfail-open exportされるため、traceの存在とSession attachmentを別々に判定する。

### 401または403

1. startupのloginまたはcurrent user取得で401または403になった場合は、`exporter_state=failed`、`last_connection_error_retryable=false`になることを確認する。
2. `retry_stopped_reason`を確認し、`buffered_spans`が0になり`dropped_spans`へ移ったことを確認する。
3. API keyの有効期限、scope、project accessを確認する。
4. `exporter_state=ready`後のOTLP 401または403も再試行されないが、現行healthからresponse codeを取得できないためSDK logを確認する。
5. 設定を修復した後にprocessを再起動する。
6. key漏えいの疑いがあれば先に失効し、新しいkeyへrotationする。
7. routing isolation canaryを再実行する。

### 429または5xx

1. startup connectorは429をjitter付きで再試行するが`Retry-After`を利用せず、ready後のOTLP HTTP exporterは429を再試行しない。
2. ready後のOTLP HTTP exporterは408と5xxを再試行する。
3. OTLP retry回数、接続後queue、最終dropはhealthから取得できないため、SDK logとGalileo側のincident情報を確認する。
4. startup中なら`buffered_spans`と`dropped_spans`を確認する。
5. content sizeとsample rateを一時的に下げる。
6. 429と`Retry-After`を配送要件にする場合はCollector profileへ移行する。
7. delivery freshnessのerror budgetを消費し続ける場合はCollectorの永続queueを検討する。

### Partial rejectionまたはschema error

1. 現行OTLP HTTP exporterはHTTP 2xx response bodyを解析しないため、`partialSuccess`のrejected span数とmessageを取得できない。
2. Galileo側のingestion logまたはread-backで欠落を確認する。
3. rejected fixtureのspan kind、必須属性、型、payload sizeを確認する。
4. Galileo SDKまたはGenAI schemaの更新履歴とgolden contract差分を確認する。
5. response本文を保存してalertする必要がある場合は、Collector profileで検出経路を追加する。
6. 修正後は新しいtrace IDでcanaryを送る。

### Conversation Qualityが計算されない

1. 対象log streamでConversation Qualityが有効かを確認する。
2. 対象native Sessionに二つ以上のturn traceが入り、各root traceに実textのinputとoutputがあることを確認する。
3. `HERMES_GALILEO_CAPTURE_CONTENT=true`と`HERMES_GALILEO_SAMPLE_RATE=1.0`を会話評価profileへ設定したことを確認する。
4. content取得の承認記録とprivacy canary結果を確認する。
5. metric sampling、judge model、計算queue、cost上限をGalileo側で確認する。
6. placeholderだけの通常profileはConversation Qualityの有用性を保証しないため、評価対象から除外する。
7. adapterのSession attachment不良とGalileo側のmetric計算失敗を別々に切り分ける。

### State evictionまたはorphan増加

1. in-flight turn数、TTL、Hermesの終了event欠落率を確認する。
2. `turn_id`、`api_request_id`、`tool_call_id`の欠落を集計する。
3. peak concurrencyが512を超えるなら、単純に上限を増やす前にturn lifecycleとmemoryをload testする。
4. idle時にTTL回収が必要なら、host側のperiodic sweepを設計する。

### Privacy incident

1. `HERMES_GALILEO_ENABLED=false`または`GALILEO_LOGGING_DISABLED=true`で新規送信を止める。
2. Galileo API keyをrotationする。
3. 影響するproject、log stream、時間範囲、trace IDを特定する。
4. Galileo側のaccessとretentionを制限し、削除手続きを開始する。
5. canaryが通過したfieldとevent種別を特定する。
6. redactor修正後、全privacy corpusとlive read-backを完了するまでcontent取得を再開しない。

## Graceful shutdown

host processは、終了signalを受けたら次の順序で処理する。

1. 新しいHermes requestの受付を止める。
2. active turnをhost側の猶予内で完了させる。
3. packageの共有runtimeをunpublishし、runtimeのhook admission gateを閉じる。
4. pending Session解決をnative Session期限まで待つ。
5. 期限切れのSession要求をcancel callbackでfail-open解放し、pending spanをSession IDなしでreplayする。
6. 未完了turnとspanをshutdown理由付きで閉じ、残ったdeferred spanをfail-open終了する。
7. Session coordinatorの新規解決要求を止め、Session workerが期限を超えて生存する場合は`native_session_worker_cleanup_deferred=true`を設定する。
8. background flush workerをflush timeoutまで待つ。
9. workerが停止した場合は、所有するTracerProviderを一度shutdownする。
10. workerが期限を超えた場合は、Provider cleanupをdaemonへ延期し、worker終了後に一度だけ実行する。
11. deferred processorは自身のflush-timeout deadline内でstartup replay完了後のreadyを待つ。
12. delegateの`force_flush`と`shutdown`をoperation lockで直列化する。
13. `force_flush`はoperation lock取得後にready状態とdelegate identityを再検証し、shutdownが先行していればdelegateを呼ばない。
14. delegate shutdownがdeadlineを超えた場合はdaemonで継続する。
15. readyにならなければbufferを`dropped_spans`へ加算して破棄する。
16. 外部注入Providerは所有しないためshutdownせず、flusher停止後に`force_flush`だけを要求する。
17. `native_session_worker_cleanup_deferred`、`connector_cleanup_deferred`、`provider_cleanup_deferred`、`delegate_cleanup_deferred`を各cleanupの期限超過判別に使う。

termination grace periodは、Agentのdrain時間、Session coordinator、flusher、deferred processorが個別に使うtimeoutの合計より長くする。
`force_flush`は指定timeout内でreadyを待ち、残り時間で公式processorへqueue drainを要求する。
`force_flush=True`はqueue drain要求の完了だけを表し、Galileoのdelivery acknowledgmentではない。
flush timeoutはOTLP export HTTP timeoutと各shutdown段階のbounded waitに使う。
processor構築前のhealth check、login、current user取得はこのtimeoutで制御せず、現行galileo-coreの既定では各requestが最大60秒待ち得る。
そのためconnector daemonはruntime shutdown後もconstructor、startup replay、またはcleanup内で継続する場合があり、その間は`connector_cleanup_deferred=true`になる。
同様に、native Sessionのlocal deadlineは公式SDKの同期`start_session()` callをcancelせず、callが残る間はworkerがdaemonで継続する。
background flusherとProvider配下のdeferred processorは別々にflush timeoutを使うため、runtime shutdown全体を一つのtimeoutに収める保証はない。
外部注入Providerの`force_flush`がtimeoutを無視する場合も同期shutdownが長引き得る。
daemon cleanup完了は各段階のdeadline内に保証しない。
packageが共有runtimeをunpublishした後は、公開`health_snapshot()`は`enabled=false`だけを返す。
deferred cleanup flagを終了中に監視するhostは、unpublish前に保持したruntime参照からsnapshotを読む。
毎spanまたは毎tool callでforce flushしない。

## E2E検証手順

### 実装済みwire E2E

`tests/e2e`のwire testはfake Galileo HTTP serverへ公式SDKを接続し、次の境界を自動検証する。

- Session検索と作成の公式API
- `/healthcheck`
- `/login/api_key`
- `/current_user`
- `/otel/traces`へのOTLP protobuf送信
- Galileo認証header
- OpenTelemetry resource、project、log stream
- HMAC external IDと`galileo.session.id`
- 同じHermes sessionに属する複数turnのnative Session共有
- Session解決のsingle-flight、pending buffer、timeout時fail-open
- root Agent、LLM、Toolのtrace IDと親子関係
- aggregate `prompt_tokens`とoutput token
- responseの`assistant_message`から作るstructured output
- content取得時のsecret秘匿とuser ID非送信
- raw Hermes session IDのSession API、span、log、healthへの非混入
- 会話履歴Opt-Inが無効なら現在のuser messageだけを送り、有効なら過去messageも送る境界
- 自由text Cookie headerとPEM private keyのwire非混入
- 自由text途中のbase64 data URIのwire非混入
- hidden reasoning key、Anthropic thinking blockとsignature、Gemini `thought_signature`のwire非混入

このtestは公式SDKを含むwireまでを対象とし、実Galileoへ保存されたnative Sessionとtraceのread-backは対象外である。
canonical requestの`body.messages`抽出とcache tokenの別属性はintegration testで検証する。
同一論理API requestのretry sequenceもintegration testで検証し、同じrequest ID、1始まりのattempt番号、失敗と成功のstatus、rootの論理request数を固定する。

### 検証環境

live E2Eには、productionと分離したGalileo project、log stream、API keyを使う。
各runへ一意な相関IDを付け、保持期間を短くする。
fixtureには実在する個人情報やcredentialを使わない。

### Contract検証

1. 一つのHermes sessionに二つのturnを作る。
2. 各turnでLLM call、tool call、approval、subagentを発生させる。
3. root spanがturnごとに別traceになり、同じ`gen_ai.conversation.id`を持つことを確認する。
4. 一つのGalileo native Sessionが作成または再利用され、両traceの`galileo.session.id`が返却UUIDと一致することを確認する。
5. external ID、`gen_ai.conversation.id`、`hermes.session.id`が同じHermes session HMACへ対応し、raw IDを含まないことを確認する。
6. span名、kind、parentage、operation、provider、model、token、finish reason、tool call ID、error typeをgolden contractと比較する。
7. 同名toolを並行実行し、stable tool call IDで分離できることを確認する。
8. 同じcommandとpatternを持つ二つのapprovalをinterleaveし、preとpostの`tool_call_id`で正しいtool parentとchoiceへ相関することを確認する。
9. `on_session_end`後の次turnは同じnative Sessionを使い、finalizeまたはreset後はlocal mappingを再利用しないことを確認する。

### Privacy検証

1. prompt、response、tool arguments、tool result、approval command、subagent summary、error messageへ別々のcanary secretを入れる。
2. content取得無効時に、inputとoutputがplaceholderになり、error messageが保存されず、status descriptionがbounded error typeだけになることを確認する。
3. content取得有効時に、既知secretが`[REDACTED]`または省略表現になり、error messageも同じredactorを通ることを確認する。
4. Toolの専用error message保存先が秘匿済みexception eventへ集約され、custom error属性を作らないことを検査する。
5. explicit error messageがなくTool resultをexception messageへfallbackするcaseでは、output属性にも同じresultが残ることを契約として検査する。
6. Cookie header、PEM private key、generic JWT、AWSとGoogleの既知access key、quoted passwordまたはsecret assignment、自由text途中のbase64 data URI、known hidden reasoning keyをcanary corpusへ含める。
7. raw Hermes session IDをSession external ID、Session metadata、span、application log、health snapshotから検索する。
8. Galileo read-back、exporter error、application log、health snapshotを検索する。
9. 一つでもcanaryまたはraw Hermes session IDが見つかった場合は失敗とする。

### Failure matrix

実装済みwire failure matrixは、fake Galileo HTTP serverから次の応答を返し、公式processorと現行OTLP HTTP exporterのcontractを固定する。

| case | 現行期待値 |
| --- | --- |
| HTTP 401 | 一回送信し、再試行しない |
| HTTP 429 | 一回送信し、再試行しない |
| HTTP 503 | 二回送信する |
| HTTP 408 | 二回送信する |
| connection reset | 二回送信する |
| read timeout | 設定したHTTP timeoutで終了し、現行fixtureでは一回送信する |
| HTTP 200 partial success | 一回送信し、rejected spanをhealthへ反映しない |
| large payload | content上限を超える文字列がwireへそのまま入らない |

再試行するcaseでは、trace ID、span ID、serialize済みOTLP request bodyが同一であることを確認する。
全caseで現行healthの`dropped_spans`がready後のfailureやpartial rejectionを表さないことも固定する。
429 fixtureはheaderなしと`Retry-After: 0`の両方を返し、どちらも再試行しない現行dependency contractを固定する。
このmatrixはDirect profileの採用dependency挙動を検証するものであり、adapterへ429 retry、`Retry-After`処理、partial success解析を実装する要件ではない。

### Loadとshutdown

1. 512件を超える同時turnを作り、最古stateがevictedとして閉じることを確認する。
2. Session APIを遅延させ、Session解決queue、pending-span buffer、capacity drop、timeout、Agent overheadを測る。
3. exporterを遅延させ、startup buffer capacity、drop、Agent overheadを測る。
4. open LLM、tool、subagentとpending Sessionを残したままSIGTERMを送る。
5. abandonedとshutdown span、Session IDなしfail-open replay、flush完了、process終了時間を確認する。
6. Collector profileを導入した場合だけ、強制終了後のWAL復元と再送を確認する。

### Live Galileo受入

1. 専用projectとlog streamへ、同じHermes sessionに属する二つ以上のcanary turnを送る。
2. bounded timeoutでGalileoをpollする。
3. HMAC external IDを持つnative Sessionが一つだけ存在し、両turn traceが同じ`galileo.session.id`へ属することを確認する。
4. project、log stream、conversation ID、trace、span parentageを確認する。
5. model、token、finish reason、tool status、error status、durationをprovider fixtureと比較する。
6. 同一trace IDとspan IDの重複と、同一external IDの重複Sessionを検索する。
7. privacy canaryとraw Hermes session IDが0件であることを確認する。
8. 通常profileのplaceholderが有効なtraceとして扱われることを確認する。
9. 会話評価profileではprivacy審査済みcontentを使い、Galileo log streamでConversation Qualityを有効化する。
10. Conversation Qualityが対象native Sessionへ計算され、Tool Errorなどの採用metricも期待するnodeへ計算されることを確認する。

この受入は外部Galileo credential、read API、log streamのmetric設定に依存する。
stub E2Eの成功だけではlive受入完了と扱わない。

## GitHub Actions運用

### Pull request checks

`.github/workflows/test.yml`は、`main`へのpush、pull request、手動実行で起動する。
Python 3.10、3.12、3.14のmatrixでlint、format check、unit、integration、公式SDK stub E2Eを実行する。
stub E2Eはsecretを使わず、すべてのpull requestで必須とする。

live E2Eは、同一repositoryの信頼済みpull request、`main`へのpush、手動実行だけを対象にする。
fork pull requestとDependabotではlive job内で明示skipし、`pull_request_target`は使わない。
trusted pull request、`main`へのpush、`test.yml`の手動runで`GALILEO_API_KEY`がない場合はfail-closedにする。
fork pull requestとDependabotではsecret不要のlocal checksを継続する。

live E2Eには次のrepository secretとvariablesを使う。

| 種別 | 名前 | 既定値 | 用途 |
| --- | --- | --- | --- |
| secret | `GALILEO_API_KEY` | なし | 専用Galileo環境の認証 |
| variable | `GALILEO_PROJECT` | `hermes-galileo-ci` | live E2E専用project |
| variable | `GALILEO_LOG_STREAM` | `github-actions-live-e2e` | live E2E専用log stream |
| variable | `GALILEO_API_URL` | なし | custom deploymentのAPI endpoint |
| variable | `GALILEO_CONSOLE_URL` | なし | custom deploymentのconsole endpoint |
| variable | `GALILEO_E2E_REQUIRE_CONVERSATION_QUALITY` | `true` | Conversation Qualityをlive受入の必須assertionにする |

CI log streamではConversation Qualityを有効化し、通常は既定値`true`のまま使う。
一時的な切り分けでこのvariableを`false`にしたrunはnative Sessionと複数turnのread-backまでを検証するが、Conversation Qualityの受入完了を証明しない。
`GALILEO_API_KEY`にはproductionと分離したCI専用projectおよびlog streamだけを操作できる最小権限のkeyを使う。
同一repositoryのbranchへpushできる主体だけをtrusted code authorとして扱い、forkとDependabotにはkeyを渡さない。
組織policyで追加承認が必要な場合はlive jobをGitHub Environmentへ割り当てられるが、その場合はpull request checkに手動承認が必要になる。

### 日次dependency watch

`.github/workflows/dependency-watch.yml`は、cron `17 20 * * *`で毎日05:17 JSTに起動し、手動実行も受け付ける。
Hermes mainのcommit SHA、PyPI project metadataが示すGalileo current release、およびpipが解決したGalileoの全依存closureを`.github/dependency-baseline.json`と比較する。
どちらも変わっておらず`force_test=false`の場合は最新版testを省略し、比較結果だけを残す。
この場合はlive E2Eとsecret確認も行わない。

更新を検出した場合または`force_test=true`の場合は、secretを持たないrunnerでHermes source contractと検出したexact Galileo dependency closureを使う全local testを実行する。
local test成功後にfresh runnerを使い、必須の`GALILEO_API_KEY`で同じexact closureのlive E2Eを実行する。
Hermes source取得、PyPI参照、live Galileoは別々の外部依存としてjob summaryへ結果を残す。
Hermes source contractは実PluginManagerによるentry point registrationと全observer hookの呼び出しまでを対象にする。
Hermes本体のpackage install、実LLMを使うAgent loop、gateway起動は対象外である。

全検証が成功した場合は、`automation/dependency-baseline` branchを明示的な`--force-with-lease`で更新し、baseline更新pull requestを作成する。
`GITHUB_TOKEN`で作成したpull requestの`pull_request` runは自動起動しない場合やapproval待ちになる場合があるため、`actions:write`を持つ限定jobから`test.yml`の`workflow_dispatch`を明示実行する。
repository policyによっては同じcommitにapproval待ちのpull request runが併存するが、明示dispatchの結果を自動更新の検証記録とする。
baseline更新以外のrepository write権限を同jobへ与えない。
repositoryのActions設定では、Workflowからpull requestを作成できるようにする。
branch protectionではPython matrixのlocal checks、`Hermes Agent compatibility`、`Galileo live E2E`をrequired checksへ登録する。

### Workflow security

GitHub Actionsは、third-party actionをcommit SHAへpinする。
jobごとに最小permissionsとtimeoutを設定する。
pull request用Workflowは同じrefの古いrunをconcurrency cancellationで停止し、dependency watchはbaseline更新競合を避けるため同じgroupを直列実行する。
secret付きjobはuntrusted pull request codeを実行しない。
fork pull requestとDependabotのlive jobはsafe skipするが、live受入済みとは扱わず、merge前に同じcommitを同一repositoryのtrusted branchで再検証する。
live E2E用projectとlog streamはproductionから分離し、fixtureには実在する個人情報やcredentialを使わない。

## Collectorへ移行する場合

Collector導入は運用設定の追加だけではなく、export pathの変更である。
導入前に[設計のADR-003](./DESIGN.md#adr-003-direct-sdkとcollectorの境界)を再評価する。

Collectorでは、memory limiterを最初のprocessorに置き、samplingとbatchをその後に配置する。
送信queue、retry、永続WAL、queue metricを有効にし、一つのtraceを同じtail-sampling instanceへrouteする。

参考資料は次のとおりである。

- [Collector Resiliency](https://opentelemetry.io/docs/collector/resiliency/)
- [Memory Limiter Processor](https://github.com/open-telemetry/opentelemetry-collector/blob/main/processor/memorylimiterprocessor/README.md)
- [Tail Sampling Processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/README.md)
- [Batch Processor](https://github.com/open-telemetry/opentelemetry-collector/blob/main/processor/batchprocessor/README.md)

## 責任分界と外部依存

Direct adapterは、次の責務を持つ。

- Hermes eventからspanへのmapping
- native Sessionの非同期作成または再利用とHMAC相関
- export前privacy処理
- startup buffer、Session解決queue、pending-span bufferの有界化
- SDK bootstrap接続とlocal health
- fail-open、flush、shutdown

Collector profileは、次の責務を持つ。

- ready後のOTLP data-plane retryと`Retry-After`
- OTLP partial successの解析とrejected span計測
- 送信queueの詳細metricと最終送信成功
- 永続WAL、crash recovery、暗号化spool
- tail samplingとtrace affinity
- 複数destinationへの集中routing

Galileoと運用基盤は、次の外部依存を持つ。

- native Sessionとtraceの保存およびread-back
- 同時Session作成時のexternal ID一意性
- Conversation Qualityを含むmetricのlog stream設定、judge実行、結果保持
- RBAC、retention、削除、data region
- HTTP形式のliveness、readiness endpoint、dashboard、alert
- GitHub Actions runner、repository secret、variables、required checks

pseudonym secretのkey version管理、rotation手順、実Galileo live E2Eはv1受入で運用者が完了させる。
Direct profileで提供しない配送機能をadapterの未充足項目として扱わない。

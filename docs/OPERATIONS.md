# Hermes Galileo連携の運用設計

- 運用基準日：2026-07-24
- 対象：direct Galileo SDK構成
- 関連文書：[要件定義](./REQUIREMENTS.md)、[設計](./DESIGN.md)

## 現行運用モデル

現行構成は、Hermes process内でOpenTelemetry spanを生成し、deferred processorを介して公式`GalileoSpanProcessor`からGalileoへ直接送信する。
deferred processorは公式SDKのhealth check、login、current user取得をdaemon threadで実行し、接続前とstartup replay中に終了したspanを最大2048件保持する。
startup replay完了後だけreadyになり、恒久errorではfailedへ遷移してretryを止める。
会話の関連づけには`gen_ai.conversation.id`を使い、Galileo native sessionは作成しない。

障害時はAgentを優先する。
telemetryの初期化、span生成、flush、exportが失敗しても、Hermesのユーザー応答を失敗させない。

## 設定

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

### hermes-galileo設定

| 環境変数 | 既定値 | 許容範囲 | 動作 |
| --- | --- | --- | --- |
| `HERMES_GALILEO_ENABLED` | `true` | boolean | 連携全体の有効化 |
| `HERMES_GALILEO_CAPTURE_CONTENT` | `false` | boolean | 秘匿済みcontentの取得 |
| `HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY` | `false` | boolean | root inputで会話履歴を優先 |
| `HERMES_GALILEO_HASH_USER_IDS` | `true` | boolean | user IDを仮名化 |
| `HERMES_GALILEO_PSEUDONYM_SECRET` | 空 | secret文字列 | user IDのHMAC key |
| `HERMES_GALILEO_MAX_CONTENT_CHARS` | `12000` | 256から1000000 | 一つのcontent文字数上限 |
| `HERMES_GALILEO_MAX_COLLECTION_ITEMS` | `100` | 1から10000 | mappingまたはsequenceの要素上限 |
| `HERMES_GALILEO_SAMPLE_RATE` | `1.0` | 0.0から1.0 | root traceのhead sampling率 |
| `HERMES_GALILEO_TURN_TTL_SECONDS` | `900` | 30から86400 | inactive turn stateのTTL |
| `HERMES_GALILEO_ASYNC_FLUSH_ON_TURN_END` | `true` | boolean | turn終了時のbackground flush |
| `HERMES_GALILEO_FLUSH_TIMEOUT_MILLIS` | `10000` | 100から120000 | force flush、OTLP export、各cleanup待ちのtimeout |
| `HERMES_GALILEO_DEBUG` | `false` | boolean | 予約済みdebug設定 |
| `HERMES_GALILEO_ENVIRONMENT` | `development` | 空でない文字列を推奨 | deployment environment属性 |
| `HERMES_GALILEO_SERVICE_NAME` | `hermes-agent` | 空でない文字列を推奨 | OpenTelemetry service name |

booleanは、`1`、`true`、`yes`、`on`と、`0`、`false`、`no`、`off`を大文字小文字を区別せず受け入れる。
それ以外の値と数値範囲外はstartup時に拒否する。

`HERMES_GALILEO_CAPTURE_CONTENT=true`は、無加工のfull captureを意味しない。
既知secretの秘匿、binary省略、collection上限、文字数上限を適用したcontentだけを送る。

`HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY`は、content取得が無効なら実内容を送らない。
content取得が有効でも履歴取得が無効なら、LLM API spanはcanonical provider requestの蓄積済み`body.messages`ではなく、別fieldの現在の`user_message`だけを送る。
履歴取得が有効な場合だけ、`body.messages`をstructured inputとして送る。

`HERMES_GALILEO_PSEUDONYM_SECRET`を設定すると、user IDはHMAC-SHA-256で仮名化される。
未設定時はGalileo API keyをHMAC keyとして使うため、productionでは相関IDのrotationを認証keyのrotationから分離する専用secretを設定する。

`HERMES_GALILEO_DEBUG`は設定として読み込むが、現行runtimeのlog levelやpayload出力を切り替えない。
debug目的でもpayloadとcredentialをlogへ出さない。

## 配備前checklist

### Security

- Galileo API keyを対象projectとlog streamに必要な最小権限で発行する。
- keyのowner、rotation期限、失効手順を記録する。
- pseudonym secretをAPI keyと分け、key versionとrotation時の相関期間を記録する。
- projectとlog streamをenvironmentごとに分ける。
- productionでcontent取得を有効にする場合は、data ownerとsecurity reviewerの承認を得る。
- GalileoのRBAC、retention、削除、data regionを確認する。
- session ID、task ID、tool名に個人情報を含めない。

### Compatibility

- installed distributionの`hermes_agent.plugins` entry pointが、callableな`register`を持つmodule objectを返すtestを実行する。
- Galileo SDK、OpenTelemetry SDK、GenAI Semantic Conventionsのversionを記録する。
- support対象の最小SDK versionと採用versionでcontract testを実行する。
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
- 接続後のSDK processor queue capacityとfull時の挙動はhealthから見えないため、採用versionの負荷試験とlive read-backで別に確認する。
- shutdown grace periodをflush timeoutより長くする。

## Health、readiness、status

現行runtimeはHTTP endpointを直接提供せず、programmatic health snapshotを提供する。
host processがKubernetesなどで動作する場合は、このsnapshotとexporter metricsからprobeを構成する。

[Kubernetes Probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/)に従い、livenessとreadinessを分ける。

### Liveness

livenessは、processとevent loopまたはworker threadが動作しているかだけを判定する。
Galileo outage、429、認証errorを理由にlivenessを失敗させない。
外部依存障害で再起動を繰り返すと、queueと未送信spanを失う可能性が増えるためである。

### Readiness

readinessは、新しい観測eventを安全に受け入れられるかを判定する。
現行runtimeはreadiness判定そのものを返さないため、host processがhealth snapshotから次の条件を評価する。

- 設定のsyntaxと必須値が検証済みである。
- runtimeとProviderが初期化済みである。
- shutdown処理へ入っていない。
- in-flight turnが512件の上限へ接近していない。
- `exporter_state`が`connecting`または`replaying`の場合は、`buffered_spans`が2048件の上限へ接近しておらず、`dropped_spans`が増加していない。

Galileo SDKを初期化中またはstartup bufferをreplay中でも、bufferに十分な余地があれば`degraded`としてreadyを維持できる。
bufferが飽和し、継続的なlossが避けられない場合はnot readyにする。
`exporter_state=failed`は自動回復しないためnot readyとし、設定またはcredentialを修復してprocessを再起動する。

`exporter_ready=true`は公式processorのconstructorとstartup replayの完了を示すが、Galileoへのbatch受理を示さない。
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

`last_connection_error_type`はclass名だけであり、error messageやcredentialを含めない。
`last_connection_error_retryable`は真偽値または未設定であり、`retry_stopped_reason`は低cardinalityの停止理由である。
`dropped_spans`はstartup bufferのoverflow、replay失敗、恒久初期化失敗、failed、stopping、stopped状態で終了したspan、shutdown期限切れを数える。
ready後のenqueue失敗、接続後のSDK queue、exportedまたはrejected span数、OTLP retry数、最終export成功時刻、export latency、partial successは含まれない。
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
| Delivery freshness | accepted spanの99.5%以上が5分以内にGalileoへ到達 | end timeからGalileo read-back time | live計測が必要 |
| Trace completeness | sampled traceの99%以上が必須spanと属性を持つ | contract validator | 測定可能 |
| Telemetry loss | queue-full、non-retryable、partial rejectionの合計が0.1%以下 | lost ÷ generated | startup dropだけ測定可能 |
| Duplicate | 重複span IDが0.1%未満 | 同一trace IDとspan IDの重複集計 | Galileo read-backが必要 |
| Privacy canary | known secretとsynthetic PIIの漏えい0件 | export前captureとGalileo read-back検索 | live計測が必要 |
| Routing isolation | 別tenantへの誤配送0件 | projectとlog stream別canary | live計測が必要 |

privacyとtenant誤配送は通常のerror budgetへ含めない。
一件でも確認した場合はsecurity incidentとして扱う。

Agent品質は配送SLOと分離する。
Action Completion、Tool Error、Agent Efficiency、Agent Flow、Conversation Quality、安全性をagent、prompt、model version別に追跡する。
Galileoのmetric定義は、[Agentic Metrics Overview](https://docs.galileo.ai/concepts/metrics/agentic/agentic-overview)を基準にする。

## Dashboardとalert

### Dashboard

現行health snapshotから、最低限次の時系列を作る。

- `exporter_ready`と`exporter_state`
- `buffered_spans`と固定上限2048に対するstartup buffer使用率
- `dropped_spans`の増分
- `connection_attempts`の増分、`last_connection_error_type`、`last_connection_error_retryable`、`retry_stopped_reason`
- `connector_cleanup_deferred`、`provider_cleanup_deferred`、`delegate_cleanup_deferred`
- in-flight turn、child span、subagent、orphaned span

Galileo read-back、SDK wrapper、またはCollectorを追加した段階で、次の時系列を同じdashboardへ加える。

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
- shutdown後もdeferred cleanup flagが運用で定めた猶予を超えて継続する。

最終export成功からの経過時間、partial rejection、HTTP 401、403、429、non-retryable schema errorは、現行healthだけではalertにできない。
これらはSDK wrapper、Collector、Galileo read-backのいずれかで観測経路を追加した後に有効化する。

state evictionはtraceまたはlogから検出し、1件以上でalertにする。
privacy canaryとrouting canaryはGalileo read-backで検出し、1件以上でalertにする。
privacy canaryとrouting canaryは即時通知する。
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

## Retryとbackpressure

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

### OTLP batch送信

現行依存関係で確認したOpenTelemetry Python 1.44.0のOTLP HTTP exporterの挙動は次のとおりである。

| 状態 | 現行動作 | 要件との差 |
| --- | --- | --- |
| connection error | exporter内部で再試行する | retry回数と最終dropをhealthから取得できない |
| HTTP 408 | exporter内部で再試行する | retry回数をhealthから取得できない |
| HTTP 500から599 | exporter内部で再試行する | retry回数をhealthから取得できない |
| HTTP 429 | 再試行しない | `Retry-After`を尊重するREL-007を満たさない |
| HTTP 2xx | response bodyを解析せず成功を返す | `partialSuccess`を検出するREL-008を満たさない |
| その他のHTTP 4xx | 再試行しない | response code別counterとalertがない |

この挙動は、[OpenTelemetry Python 1.44.0 OTLP HTTP trace exporter](https://github.com/open-telemetry/opentelemetry-python/blob/v1.44.0/exporter/opentelemetry-exporter-otlp-proto-http/src/opentelemetry/exporter/otlp/proto/http/trace_exporter/__init__.py)と[同versionのHTTP retry判定](https://github.com/open-telemetry/opentelemetry-python/blob/v1.44.0/exporter/opentelemetry-exporter-otlp-proto-http/src/opentelemetry/exporter/otlp/proto/http/_common/__init__.py)で確認した。
現在のdependency constraintは`galileo[otel]>=2.5.1,<3`であり、OpenTelemetry 1.44.0を直接pinしていないため、依存更新時は同じwire contractを再確認する。

OTLPの目標動作は、429で`Retry-After`を尊重し、一時errorだけを有界に再試行し、partial successのrejected数をlossとして記録することである。
現行direct構成はこの目標を満たさないため、REL-007とREL-008を未充足として扱う。

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
6. 429対応が必要ならprocessorまたはexporter wrapper、もしくはCollector profileを導入する。
7. delivery freshnessのerror budgetを消費し続ける場合はCollectorの永続queueを検討する。

### Partial rejectionまたはschema error

1. 現行OTLP HTTP exporterはHTTP 2xx response bodyを解析しないため、`partialSuccess`のrejected span数とmessageを取得できない。
2. Galileo側のingestion logまたはread-backで欠落を確認する。
3. rejected fixtureのspan kind、必須属性、型、payload sizeを確認する。
4. Galileo SDKまたはGenAI schemaの更新履歴とgolden contract差分を確認する。
5. response本文を保存してalertする必要がある場合は、processorまたはexporter wrapper、もしくはCollectorで検出経路を追加する。
6. 修正後は新しいtrace IDでcanaryを送る。

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
4. 未完了spanをshutdown理由付きで閉じる。
5. background flush workerをflush timeoutまで待つ。
6. workerが停止した場合は、所有するTracerProviderを一度shutdownする。
7. workerが期限を超えた場合は、Provider cleanupをdaemonへ延期し、worker終了後に一度だけ実行する。
8. deferred processorは自身のflush-timeout deadline内でstartup replay完了後のreadyを待つ。
9. delegateの`force_flush`と`shutdown`をoperation lockで直列化する。
10. `force_flush`はoperation lock取得後にready状態とdelegate identityを再検証し、shutdownが先行していればdelegateを呼ばない。
11. delegate shutdownがdeadlineを超えた場合はdaemonで継続する。
12. readyにならなければbufferを`dropped_spans`へ加算して破棄する。
13. 外部注入Providerは所有しないためshutdownせず、flusher停止後に`force_flush`だけを要求する。
14. `connector_cleanup_deferred`を期限後のconnector継続、`provider_cleanup_deferred`を期限後のProvider cleanup、`delegate_cleanup_deferred`をdelegate cleanup進行中の判別に使う。

termination grace periodは、Agentのdrain時間とflusherおよびdeferred processorが個別に使うflush timeoutの合計より長くする。
`force_flush`は指定timeout内でreadyを待ち、残り時間で公式processorへqueue drainを要求する。
`force_flush=True`はqueue drain要求の完了だけを表し、Galileoのdelivery acknowledgmentではない。
flush timeoutはOTLP export HTTP timeoutと各shutdown段階のbounded waitに使う。
processor構築前のhealth check、login、current user取得はこのtimeoutで制御せず、現行galileo-coreの既定では各requestが最大60秒待ち得る。
そのためconnector daemonはruntime shutdown後もconstructor、startup replay、またはcleanup内で継続する場合があり、その間は`connector_cleanup_deferred=true`になる。
background flusherとProvider配下のdeferred processorは別々にflush timeoutを使うため、runtime shutdown全体を一つのtimeoutに収める保証はない。
外部注入Providerの`force_flush`がtimeoutを無視する場合も同期shutdownが長引き得る。
daemon cleanup完了は各段階のdeadline内に保証しない。
packageが共有runtimeをunpublishした後は、公開`health_snapshot()`は`enabled=false`だけを返す。
deferred cleanup flagを終了中に監視するhostは、unpublish前に保持したruntime参照からsnapshotを読む。
毎spanまたは毎tool callでforce flushしない。

## E2E検証手順

### 実装済みwire E2E

`tests/e2e/test_otlp_pipeline.py`はfake Galileo HTTP serverへ公式SDKを接続し、次の境界を自動検証する。

- `/healthcheck`
- `/login/api_key`
- `/current_user`
- `/otel/traces`へのOTLP protobuf送信
- Galileo認証header
- OpenTelemetry resource、project、log stream
- root Agent、LLM、Toolのtrace IDと親子関係
- aggregate `prompt_tokens`とoutput token
- responseの`assistant_message`から作るstructured output
- content取得時のsecret秘匿とuser ID非送信
- 会話履歴Opt-Inが無効なら現在のuser messageだけを送り、有効なら過去messageも送る境界
- 自由text Cookie headerとPEM private keyのwire非混入
- 自由text途中のbase64 data URIのwire非混入
- hidden reasoning key、Anthropic thinking blockとsignature、Gemini `thought_signature`のwire非混入

このtestは公式SDKを含むwireまでを対象とし、実Galileoへ保存されたtraceのread-backは対象外である。
canonical requestの`body.messages`抽出とcache tokenの別属性はintegration testで検証する。
同一論理API requestのretry sequenceもintegration testで検証し、同じrequest ID、1始まりのattempt番号、失敗と成功のstatus、rootの論理request数を固定する。

### 検証環境

live E2Eには、productionと分離したGalileo project、log stream、API keyを使う。
各runへ一意な相関IDを付け、保持期間を短くする。
fixtureには実在する個人情報やcredentialを使わない。

### Contract検証

1. 一つのsessionに二つのturnを作る。
2. 各turnでLLM call、tool call、approval、subagentを発生させる。
3. root spanがturnごとに別traceになり、同じ`gen_ai.conversation.id`を持つことを確認する。
4. Galileo native sessionが作られることは現行の期待値に含めない。
5. span名、kind、parentage、operation、provider、model、token、finish reason、tool call ID、error typeをgolden contractと比較する。
6. 同名toolを並行実行し、stable tool call IDで分離できることを確認する。
7. 同じcommandとpatternを持つ二つのapprovalをinterleaveし、preとpostの`tool_call_id`で正しいtool parentとchoiceへ相関することを確認する。

### Privacy検証

1. prompt、response、tool arguments、tool result、approval command、subagent summary、error messageへ別々のcanary secretを入れる。
2. content取得無効時に、inputとoutputがplaceholderになり、error messageが保存されず、status descriptionがbounded error typeだけになることを確認する。
3. content取得有効時に、既知secretが`[REDACTED]`または省略表現になり、error messageも同じredactorを通ることを確認する。
4. Toolの専用error message保存先が秘匿済みexception eventへ集約され、custom error属性を作らないことを検査する。
5. explicit error messageがなくTool resultをexception messageへfallbackするcaseでは、output属性にも同じresultが残ることを契約として検査する。
6. Cookie header、PEM private key、generic JWT、AWSとGoogleの既知access key、quoted passwordまたはsecret assignment、自由text途中のbase64 data URI、known hidden reasoning keyをcanary corpusへ含める。
7. Galileo read-back、exporter error、application log、health snapshotを検索する。
8. 一つでもcanaryが見つかった場合は失敗とする。

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
この429 fixtureは`Retry-After` headerを返さないため、同headerを利用しない事実はdependency実装の調査結果であり、このmatrix自体の検証範囲ではない。
このmatrixは現在の挙動を検証するものであり、429、`Retry-After`、partial successに関するREL-007とREL-008を充足させるものではない。

### Loadとshutdown

1. 512件を超える同時turnを作り、最古stateがevictedとして閉じることを確認する。
2. exporterを遅延させ、queue capacity、drop、Agent overheadを測る。
3. open LLM、tool、subagentを残したままSIGTERMを送る。
4. abandonedとshutdown span、flush完了、process終了時間を確認する。
5. 永続spoolを導入したprofileでは、強制終了後の再起動と再送を確認する。

### Live Galileo受入

1. 専用projectとlog streamへcanary traceを送る。
2. bounded timeoutでGalileoをpollする。
3. project、log stream、conversation grouping、trace、span parentageを確認する。
4. model、token、finish reason、tool status、error status、durationをprovider fixtureと比較する。
5. 同一trace IDとspan IDの重複を検索する。
6. privacy canaryが0件であることを確認する。
7. placeholder状態とredacted content状態の両方で、Galileoがspanを有効として扱うかを記録する。
8. Tool Errorなど決定的に評価できるAgentic Metricを確認する。

この受入は外部Galileo credentialとread APIに依存し、現行repositoryの自動E2Eでは完了していない。

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

## 現行制約

次の項目は、現在利用できる運用機能ではない。

- Galileo native sessionのprovisioningとlifecycle管理
- 接続後のSDK queueのsize、capacity、oldest age
- partial rejectionの専用counter
- ready後のOTLP HTTP 429再試行と`Retry-After`対応
- 最終export成功時刻とdelivery latency
- ready後のdelegate enqueue失敗counter
- process強制終了後のdeferred daemon cleanup保証
- 永続spoolとcrash recovery
- tail sampling
- pseudonym secretのkey version管理とrotation手順
- tool argumentsとtool resultの切り詰め後JSON構造保証
- HTTP形式のlivenessとreadiness endpoint
- 実Galileoへ保存されたtraceの自動read-back E2E

これらを前提とするSLOは、対応するmetricまたは機能を追加するまで「測定不能」または「未充足」として扱う。

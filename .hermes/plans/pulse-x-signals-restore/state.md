# STATE: Pulse X likes/home timeline restoration
updated: 2026-07-13T17:46:36Z

## 目的
Pulseに本人Xアカウント `@Y20010920T` のlikesとFollowing timelineを復活させる。X Developer APIの自動チャージは再開せず、Developer APIを使わない。

## 確定事実
- repo: `/Users/akitani/.hermes/hermes-pulse`, branch `main`, baseline HEAD `e39316a4cd4acc21396461b8e0c66e4d3f77f9c1`。
- 旧`XUrlConnector`は`liked_tweets`と`timelines/reverse_chronological`を各`max_results=100`でX Developer APIから取得していた。
- X Developer残高は`$46.55`、auto rechargeはOFF。xAI auto top-upもOFF。
- 公開PostのoEmbed/Web indexでは、本人likesとFollowing timelineというprivate/account stateを代替できない。
- 本人identityはChrome Profile 4の`@Y20010920T`のみ。
- repoは他タスク由来のdirty tree。今回のcommitはhunk/pathを分離する。

## 実装決定
- bookmarksは復活対象に含めない。
- X Developer API/xurlは使わず、ログイン済みChrome Profile 4からX Web UIをheadless CDP pipeで読む。
- 専用profile `/Users/akitani/.hermes/browser/x-pulse-profile`へ、`Local State`、`Preferences`、`Secure Preferences`、X/Twitter domainのcookieだけをコピーする。他domainのcookieは除去する。
- Chrome profileは毎回refreshし、収集時にnavigationのProfile linkを`@Y20010920T`へ固定してidentity mismatch/login expiryでfail closedする。
- homeは`Following`/`フォロー中` tabを明示選択する。For Youは収集しない。
- 1 signalあたり20件、最大8 scrollに制限する。
- 取得modeは`browser_automation_experimental`として明示する。
- Production wrappers 3本は`--x-browser-signals likes,home_timeline_reverse_chronological`を使用し、`xurl auth`/`refresh-x-oauth2`を実行しない。

## Files touched
- `.hermes/plans/pulse-x-signals-restore/state.md`
- `src/hermes_pulse/connectors/x_browser.py`
- `src/hermes_pulse/cli.py`
- `src/hermes_pulse/direct_delivery.py`
- `src/hermes_pulse/launchd.py`
- `tests/test_x_browser_connector.py`
- `tests/test_cli_morning_digest.py`
- `tests/test_direct_delivery.py`
- `tests/test_launchd_integration.py`
- `/Users/akitani/Library/LaunchAgents/run-hermes-pulse-digest-direct-delivery.sh`
- `/Users/akitani/Library/LaunchAgents/run-hermes-pulse-direct-delivery.sh`
- `/Users/akitani/Library/LaunchAgents/run-hermes-pulse-evening-direct-delivery.sh`

## 検証
- Current dirty worktree targeted: 63 passed。
- Isolated baseline branch targeted: 59 passed、full suite: 281 passed。
- Live connector after X-only cookie filtering: likes 3 + Following timeline 3、計6件、unique 6。
- Live archive: likes 20 + Following timeline 20、計40件、errors `{}`、`x_signals` successful、unique raw IDs 40。
- Archive readback: `/Users/akitani/.hermes/tmp/pulse-x-verify-archive/2026-07-14-browser-signals/metadata/x-browser-verification.json`。
- Wrapper 3本: `zsh -n` pass、browser signal各1、`xurl auth`なし、metered `--x-signals`なし。
- Chrome cleanup: `/Users/akitani/.hermes/browser/x-pulse-profile`を使うactive process 0。
- Current dirty worktree full suite: 387 passed / 1 unrelated failure。別タスクの未commit source registry変更で`location_context`が外れているため`test_review_trigger_quality_surfaces_stale_inputs_from_runtime_state`が失敗。今回commitを分離したclean branchでは281/281 pass。

## 残るリスク
- X Web UI DOMの変更で将来壊れ得る。失敗は`source_errors["x_signals"]`として可視化し、Developer APIへsilent fallbackしない。
- X loginが期限切れの場合はProfile 4の再ログインが必要。別identityには切り替えない。
- 専用profileは認証情報を含むためmode 0700、cookie DB 0600。Git管理しない。

## 完了条件
- Targeted tests、X-only cookie profile、live likes/Following collection、archive readback、wrapper syntax/readback、process cleanup、exact-hunk commit/push/remote readback。

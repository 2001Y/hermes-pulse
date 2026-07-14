# STATE: Pulse X official incremental signals
updated: 2026-07-14T04:35:00Z

## 目的
Pulseへ本人`@Y20010920T`の新規likesと「フォロー中」タイムライン更新だけを、X公式API/X Activity APIで最大100件ずつ復活させる。browser Cookieは使わず、bookmarksは対象外にする。

## 確定事実
- 本人identityはX User ID `3102332970`、username `Y20010920T`。2026-07-14に`xurl --auth oauth2 --username Y20010920T whoami`でreadback済み。
- X Activity `like.create` outbound subscriptionは作成済みで、subscription listから`active`をreadbackした。
- X API live read/streamは現在`SpendCapReached`。spend cap変更は行わず、ユーザー指示によりlive有効化は一旦停止する。
- reverse chronological timelineは`since_id`と`max_results=100`を利用でき、返却数は0〜100件。
- likes listには`since_id`がない。新規likesはXAA `like.create` / `direction=outbound`で受信し、定期的にliked Posts上位100件を照合する。
- Pulse archive source ledgerはURL identityで既取得を除外するが、API取得後の処理なのでAPI費用削減にはならない。
- X Developer Auto RechargeはOFFのまま。ユーザーは2026-07-14に実装・有効化を明示承認した。
- 作業worktreeは`/Users/akitani/.hermes/tmp/hermes-pulse-x-official`、branch `pulse-x-official-incremental`。browser collector commit `ba60f90`を`git revert --no-commit`した状態から作業する。

## 決定事項
- 決定: signalsはlikesと`home_timeline_reverse_chronological`だけ。bookmarksは無効。
- 決定: timeline cursorはSlack delivery成功後にだけ進め、次回API requestへ`since_id`として渡す。
- 決定: timelineは1回最大100件。100件未満なら返却分だけ、0件なら追加なし。
- 決定: likesはXAA streamを主経路、liked Posts 100件の定期照合を取りこぼし補完にする。
- 決定: public liked Post本文は公式oEmbedでhydrateし、protected等は定期照合で補完する。
- 決定: XAA、timeline、likes照合の失敗は観測可能にし、browserや別課金APIへsilent fallbackしない。

## Files touched
- src/hermes_pulse/archive.py
- src/hermes_pulse/collection.py
- src/hermes_pulse/connectors/x_url.py
- src/hermes_pulse/connectors/x_activity_likes.py
- src/hermes_pulse/x_activity_stream.py
- src/hermes_pulse/cli.py
- src/hermes_pulse/direct_delivery.py
- src/hermes_pulse/launchd.py
- tests/test_x_url_connector.py
- tests/test_x_activity_likes.py
- tests/test_x_activity_stream.py
- tests/test_cli_morning_digest.py
- tests/test_direct_delivery.py
- tests/test_launchd_integration.py
- .hermes/plans/pulse-x-official-incremental/state.md
- live/chezmoi LaunchAgent wrappers and XAA plist/script (exact files after renderer decision)

## 未解決 / リスク
- XAA event単価は公開pricing表に明記されていない。subscription作成後のusage readbackで確認する。
- XAA stream断が5分を超える場合はイベント欠落可能性があるため、24時間ごとのliked Posts 100件照合を残す。
- 1区間でtimeline更新が100件を超える場合、ユーザー指定により最新100件を上限とし、それ以前は取得しない。

## 次の一手
1. review findingを解消してfull suiteを再実行する。
2. repo/dotfilesをexact-path commit/pushする。
3. production wrapper/stream LaunchAgentは正本まで作成し、ユーザー指示どおりlive適用・canaryを保留する。

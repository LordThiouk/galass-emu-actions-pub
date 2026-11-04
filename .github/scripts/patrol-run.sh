#!/usr/bin/env bash
# Lancé par android-emulator-runner (qui exécute 'script:' ligne par ligne et casse les '\').
set +e
flutter pub get >/dev/null
adb emu geo fix -17.4677 14.7167
adb logcat -c || true
adb logcat -v time > logcat.txt 2>&1 &
LOGCAT_PID=$!
rc=0
: > patrol-test.log
for T in rider_home_destination_test rider_multistop_test; do
  echo "=== patrol test $T ==="
  timeout 1500 patrol test -t patrol_test/$T.dart --verbose \
    --dart-define=E2E=true \
    --dart-define=API_BASE_URL="$BASE" \
    --dart-define=RIDER_TEST_PHONE="$RIDER_TEST_PHONE" \
    --dart-define=RIDER_TEST_OTP="$RIDER_TEST_OTP" 2>&1 \
    | tee -a patrol-test.log \
    | sed -E 's#https?://[^ ]+#<url>#g; s#AIza[0-9A-Za-z_-]+#<redacted>#g' \
    | tail -n 200
  r=${PIPESTATUS[0]}
  echo "patrol test $T exit code: ${r}"
  [ "$r" -ne 0 ] && rc=$r
done
echo "patrol test exit code: ${rc}"
kill "$LOGCAT_PID" 2>/dev/null
wait "$LOGCAT_PID" 2>/dev/null
if [ "$rc" -eq 0 ] && grep -qE "FAILED|Test failed|AssertionError" patrol-test.log; then
  echo "::error::patrol log contains failures despite exit 0"; rc=1
fi
exit "$rc"

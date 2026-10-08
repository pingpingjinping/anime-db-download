# Anissia subtitle discovery and URL waiting

Weekly GitHub discovery still reads the paginated work list, but only requests
captions for active/upcoming works, works with unknown status/dates, and works
that ended within the last 90 days. An `END` work without a valid end date is
historical only when its valid start date is more than 365 days old. Historical
caption APIs are skipped, including historical entries in the discovery API
retry map. AniAll discovery and monthly catalog/matching updates are unchanged.

The Pi already stores blank-URL captions in `waiting_url`, including entries
imported from the one-time historical backfill. After consuming weekly queue
Releases, the downloader groups these saved entries by Anissia work ID and
checks only those caption APIs. Each work is checked once per KST calendar
month, on the first weekly Pi run with waiters for that work. Further weekly
runs skip that API until the next month. An API failure also records the month's
attempt and keeps the waiters intact; the request helper retries transient
failures before recording the result.

Only the same work, episode, and creator can resolve a waiter. Distinct URLs for
that identity remain ambiguous and waiting. A newly available URL is saved
before download, then handed to the existing downloader/Erulabo browser queue.
If the process stops between discovery and download, the next run resumes the
saved URL without querying the API again. Empty URLs never enter the three-month
download failure/retirement policy.

Deployment needs only a pull of `anime-db-download` on the Pi. The existing
Sunday weekly timer performs monthly URL checks; no additional timer or
dependency is required. Existing version-1 state migrates automatically. Do not
delete the Pi state or repeatedly run the all-history backfill. This policy
tracks known missing URLs rather than rediscovering newly added captions on
long-finished historical works.

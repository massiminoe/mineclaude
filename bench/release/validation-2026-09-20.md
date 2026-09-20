# Source release validation — September 20, 2026

Implementation commit: `6123ab1` on `release/readiness`. The following follow-up
changes only record validation and remove a whitespace-only context line from
the reconstructed historical patch (updating its checksum and derived inventory).

- Python suite: **281 passed, 1 skipped**. The skipped test is the opt-in real
  Minecraft test, which was run separately below.
- Frontend: lint and production build passed; locally generated icons and
  third-party JavaScript notices are included in the built monitor.
- Bridge: Java 21 / Gradle build passed. Both binary and source JARs contain
  the original-code MIT license and the exact Java-WebSocket 1.5.7 MIT notice.
  Existing Gradle deprecation warnings remain; this is not a Gradle 9 build.
- Python wheel: built and verified to contain the root MIT license.
- Generated skill docs: no changes. Shell syntax checks passed.
- GitHub CI: Python, frontend, and bridge jobs all passed on the implementation
  commit: [PR workflow](https://github.com/massiminoe/mineclaude/actions/runs/35499361262).
  Consult [PR #2](https://github.com/massiminoe/mineclaude/pull/2) for checks on its latest revision.
- Benchmark inventory: 86 unique run records; 61 candidates in 14 groups,
  24 exclusions, and 1 requiring review. Twelve inventory/configuration tests passed.
- Targeted credential-pattern scan: no findings in 196 nonignored text files.
  This is not a complete security audit and does not cover ignored run artifacts.

## Real Minecraft smoke

**Passed** using the fresh release Python environment and the native arm64
client on Docker Desktop. Minecraft 1.21.5 started in a new isolated world.
The client image was rebuilt; the existing server image was reused. Exact
image identities and server mod filenames are in
[`environment-2026-09-20.json`](environment-2026-09-20.json) under `minecraft_smoke_test`.
The registry resolutions elsewhere in that file are separate observations.

The test initialized MCP and checked all eight tools, gave the bot one oak log
through server-side scenario setup, then crafted four planks through MCP.
It verified the resulting inventory through execution, `get_state`, and the
monitor API, and received a screenshot. No provider credentials or model call
were required. All temporary containers and volumes were removed and their
absence verified. This does not claim an amd64 client runtime smoke test.

Local diagnostic evidence is retained (ignored by Git) at
`state/e2e/mineclaude-e2e-5eebce52b7/`, with the test transcript at
`state/release-audit/e2e-final.log`. An initial attempt exposed a response-parsing
mistake in the new test; the passing run used the corrected test.

## Publication work still separate

The results article, final cohort decisions, dataset artifact/redaction review,
Hugging Face upload, YouTube upload, and release tag are not completed by these
repository checks. Third-party Minecraft content and the removed skin remain
in historical recordings/commits; the original-code MIT license does not apply
to that content.

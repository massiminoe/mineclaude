# Third-party components and assets

The root MIT license covers Mineclaude's original code. The components below
retain their own licenses; this document does not relicense them. This release
publishes source and build recipes, not prebuilt Minecraft images or mod bundles.

## Runtime and build components

- Minecraft Java Edition, its server, and game assets belong to Mojang/Microsoft.
  Setup downloads them separately. See the [Minecraft EULA](https://www.minecraft.net/eula)
  and [Usage Guidelines](https://www.minecraft.net/usage-guidelines). The server
  recipe sets `EULA=TRUE`; use it only if you accept those terms. Mineclaude is
  not an official Minecraft product and is not approved by or associated with Mojang or Microsoft.
- [itzg/docker-minecraft-server](https://github.com/itzg/docker-minecraft-server/blob/master/LICENSE):
  Apache-2.0 for the image's project code; bundled software retains its own terms.
- [HeadlessMC](https://github.com/headlesshq/headlessmc/blob/main/LICENSE) and
  [hmc-specifics](https://github.com/headlesshq/hmc-specifics/blob/main/LICENSE): MIT.
- [Fabric Loader](https://github.com/FabricMC/fabric-loader/blob/master/LICENSE),
  [Fabric API](https://github.com/FabricMC/fabric-api/blob/26.3/LICENSE), and
  [Fabric Language Kotlin](https://github.com/FabricMC/fabric-language-kotlin/blob/master/LICENSE): Apache-2.0.
- [Fabric Tailor](https://github.com/samolego/FabricTailor/blob/master/LICENSE): LGPL-3.0.
- [Baritone](https://github.com/cabaletta/baritone/blob/v1.14.0/LICENSE): LGPL-3.0.
  Baritone provides navigation and mining assistance and is downloaded as a separate mod.
- [Java-WebSocket 1.5.7](https://github.com/TooTallNate/Java-WebSocket/blob/v1.5.7/LICENSE):
  MIT, copyright Nathan Rajlich. This library is nested in the bridge JAR.
  Its exact upstream license is in `mc-mod/licenses/Java-WebSocket-1.5.7.txt`
  and packaged under `META-INF/licenses/` in our binary and source JARs.
- [Gradle](https://github.com/gradle/gradle/blob/master/LICENSE): Apache-2.0.
  The checked-in wrapper bootstraps the pinned Gradle distribution.

The monitor ships React, React DOM, and Scheduler notices in
`frontend/public/THIRD_PARTY_NOTICES.txt` (copied into the production build).

Python dependencies are listed in `pyproject.toml` and `requirements-dev.lock`;
frontend dependencies in `frontend/package-lock.json`. React and Vite declare MIT; TypeScript declares Apache-2.0. Installed dependency license
metadata is recorded in `bench/release/environment-2026-09-20.json`. Consult each
package's actual license when redistributing it, including transitive packages.

Provider CLI/SDK harnesses are installed separately from upstream packages in
`bench/harness/*/Dockerfile`. Their terms and account authentication are separate
from Mineclaude's MIT license. Their observed historical versions and the current
registry resolutions are recorded in the environment snapshot, with provenance.

## Assets

`frontend/public/itemIcons.json` is generated locally before frontend development
or production builds by `scripts/gen_item_icons.mjs`. It contains Minecraft
1.21.5 textures obtained through `minecraft-assets` 1.17.0, whose JavaScript
package declares MIT; that declaration does not establish a license to Minecraft
textures. Generated textures are ignored by Git and are outside this project's
MIT grant. They are still present in a locally built monitor; review Minecraft's
terms before distributing that monitor or deploying it publicly.

The former `skins/claude_crab.png` and its default texture URL were removed from
the release tree because its provenance was unverified. No custom skin is shipped
or applied by default. Historical Git commits and gameplay recordings still
contain the old skin; removing it here does not change those historical artifacts.
Users may configure their own permitted texture via `SKIN_TEXTURE_URL`.

Gameplay recordings and benchmark data are separate publication artifacts.
Their dataset terms must describe third-party content separately from original
run metadata. Do not label all artifacts MIT by inheritance from this repository.

## Reproducibility

Pinned client mod URLs are in `mc-client/download-mods.sh`; bridge versions are
in `mc-mod/gradle.properties`. The release environment snapshot records registry
image digests and dependency versions as of its capture date. It is an observation,
not a claim that older benchmark images used those digests, or that floating tags
will resolve identically later. Consult individual run metadata and version logs
for historical evidence. Prebuilt image distribution would require a separate
review of bundled software, notices, and applicable source obligations.

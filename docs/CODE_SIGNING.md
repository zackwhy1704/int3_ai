# Code signing the int3 launcher (Windows)

**Status: options and a recommendation. Nothing has been bought, enrolled or signed.**

## Why it matters now

The launcher ships as an unsigned Tauri NSIS installer (`int3_<version>_x64-setup.exe`). On Windows 11
that gives two different outcomes:

| Smart App Control (Windows Security › App & browser control) | Unsigned installer |
|---|---|
| **Off** or **Evaluation** | SmartScreen "Windows protected your PC" → **More info → Run anyway** works |
| **On** (enforcement) | **Blocked, with no override.** Seen on the dev PC on 2026-11-24: an unsigned binary failed with "An Application Control policy has blocked this file" |

Microsoft's rule: Smart App Control "will block apps and binary files identified as unsafe by Microsoft app
intelligence services unless those files are code signed with a certificate issued by a certificate
authority (CA) within the Trusted Root Program", and "unknown, unsigned code [is] blocked by default"
([Smart App Control overview](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/overview),
updated 2026-10-06). It "supports both RSA and elliptic curve cryptography (ECC) code signing
certificates" and "only considers certificates issued by trusted providers"; Artifact Signing "is the
preferred way to sign your app"
([Sign your app for Smart App Control](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/code-signing-for-smart-app-control),
updated 2026-09-28).

Signing fixes Smart App Control. It does **not** remove the SmartScreen prompt on day one with any
certificate: reputation builds over consecutive releases signed with the same identity, and EV no longer
skips it (since 2024)
([Code signing options](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options),
updated 2026-11-10).

**What must be signed:** every PE file we ship, not just the installer: the NSIS `-setup.exe`, the
installed `int3.exe`, and the uninstaller. Check the build output (`Get-AuthenticodeSignature`) for any
other `.exe`/`.dll` in the bundle, and test the installed app on a Smart App Control **On** VM.

## The options

| | Azure Artifact Signing (formerly Trusted Signing) | OV certificate from a CA | EV certificate |
|---|---|---|---|
| Smart App Control accepts it | Yes (Microsoft's preferred route) | Yes, if the CA is in the Microsoft Trusted Root Program | Yes (same as OV) |
| **Singapore company** | **Eligible** for Public Trust ¹ | Eligible | Eligible |
| **Singapore individual** | **Not eligible**: individuals must be in the USA or Canada ¹ | Depends on the CA (some issue to individuals) | Companies only, normally |
| Cost | About US$9.99/month (Basic) ² | About US$150–300/year ² | US$400+/year ² |
| Key storage | Microsoft-managed; nothing to hold | HSM or hardware token required since June 2023 (USB token or the CA's cloud HSM) ² | Same as OV |
| Identity check | Organization: legal entity, website, a monitored email on the company's domain, a business identifier (the UEN), address; 1–30 business days ¹ | CA validates the legal entity; several business days ² | Stricter than OV |
| SmartScreen | Reputation builds over time ² | Same ² | Same as OV since 2024: not worth the premium ² |
| Build integration | Tauri `signCommand` + `artifact-signing-cli`; works from any CI or this PC ³ | Tauri `certificateThumbprint` (cert in the Windows store) or `signCommand` with the CA's signing tool ³ | As OV |

¹ [Quickstart: Set up Artifact Signing](https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart)
(updated 2026-11-20): "Public Trust certificates are available to organizations in the United States,
Canada, the European Union, the United Kingdom, Australia, New Zealand, Japan, South Korea, **Singapore**,
Switzerland, Norway, and Israel. **Individual developers must be located in the United States or
Canada.**" The older comparison page still lists only USA/Canada/EU/UK for organizations; the quickstart
is newer and specific to the service. Some Microsoft Q&A answers mention a three-year business-history
requirement for organizations; that is **not** in the quickstart, so check it before enrolling a new
company.
² [Code signing options for Windows app developers](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options).
³ [Tauri: Windows code signing](https://v2.tauri.app/distribute/sign/windows/).

Also possible later, not now: publishing the launcher through the **Microsoft Store as MSIX**, which Microsoft
signs for free (²). The launcher writes into Hermes's own folders, so an MSIX build would need the same
unvirtualized-resources capability Hermes uses; that is a packaging project of its own.

## How it plugs into our build

`launcher/src-tauri/tauri.conf.json`, under `bundle.windows`:

**Artifact Signing** (the service principal's `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`
come from the environment, never the repo):

```json
"signCommand": "artifact-signing-cli -e https://<region>.codesigning.azure.net -a <account> -c <certificate-profile> -d int3 %1"
```

Tauri replaces `%1` with each file it signs (`cargo install artifact-signing-cli`). Pick a region from the
quickstart's table: none is in Singapore; Japan East or Korea Central are the nearest.

**OV certificate**, with the key in the Windows certificate store (a token or the CA's cloud HSM client):

```json
"certificateThumbprint": "<thumbprint>",
"digestAlgorithm": "sha256",
"timestampUrl": "<the CA's RFC 3161 timestamp URL>"
```

or a `signCommand` that calls the CA's own cloud-signing tool with `%1`.

After `tauri build`, look for "Successfully signed:" for each artifact, then check:

```powershell
Get-AuthenticodeSignature .\src-tauri\target\release\bundle\nsis\int3_*_x64-setup.exe
Get-ChildItem "$env:LOCALAPPDATA\int3" -Recurse -Include *.exe,*.dll | Get-AuthenticodeSignature   # after installing (per-user install folder; confirm the path)
```

Both must say `Valid`. Finally, install and run on a Windows 11 VM with Smart App Control **On**.

## Recommendation

**If InT AI signs as a registered Singapore company: Azure Artifact Signing, Basic tier, Public Trust.**
It is Microsoft's own route for Smart App Control, the cheapest (about US$120/year), has no key to look
after, and drops into Tauri as one `signCommand` line. Enrol the company (UEN, website, a domain email),
allow up to 30 business days for validation, and confirm any business-history requirement first.

**If the signer is an individual** (Artifact Signing is closed to individuals outside the USA and Canada):
an **OV certificate** from a Trusted Root Program CA that issues to individuals, with the key in the
CA's cloud HSM so signing works without a USB token.

**Not EV:** since 2024 it buys nothing over OV for SmartScreen.

Until a signed build ships, the partner guides tell anyone with Smart App Control **On** to stop and
contact us rather than switch it off.

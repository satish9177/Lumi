# Lumi judge demo checklist

Use only [the committed fictional hospital appointment](demo/fictional-hospital-appointment.html). It contains no real patient, provider, account, or appointment data.

## Live GPT-5.6 hospital appointment demo

### Prepare

Requirements: Windows 10 or 11 x64, Git, a current Node.js LTS release, npm, an OpenAI API key, and network access.

```powershell
git clone https://github.com/satish9177/Lumi.git
Set-Location Lumi
npm.cmd ci
$env:OPENAI_API_KEY = "your-key-for-this-shell-only"
Start-Process .\docs\demo\fictional-hospital-appointment.html
npm.cmd run dev
```

The key remains in Electron main. Do not put a real key in source, `.env.example`, screenshots, logs, or the demo video.

### Run the hero path

1. Keep the fictional appointment visible in its browser window.
2. Open Lumi's floating companion and wait for **Listening**. If it does not connect automatically, open **Settings** → **Voice** and choose **Connect voice**.
3. Choose the circular capture control labelled **Capture screen**. Under **Application windows**, select the browser window showing the fictional appointment.
4. Choose **Capture screen** again. Confirm that Lumi shows a local preview and has not yet shown a GPT-5.6 brief.
5. In the separate **GPT-5.6 REVIEW** card, choose **Review this capture with GPT-5.6**. This is the approval that permits the one retained capture to leave the device.
6. Verify the resulting brief stays grounded in the visible page and identifies:
   - Friday, 14 August 2026 at 9:30 AM;
   - arrival by 9:00 AM;
   - the visible preparation instruction for the prior evening;
   - the fictional referral/identification items; and
   - only the visible `https://example.com/lumi-demo/appointment` link.
7. Choose **Open extracted link**, inspect the confirmation card, then choose **Cancel**. Verify Lumi reports: `Cancelled. Nothing was changed, opened, or sent.` The browser must not navigate.
8. Optional reminder check: type `Create a reminder for this appointment.` Inspect the proposed title, time, and source context before choosing **Create reminder** or **Cancel**. Do not confirm if the proposal is not grounded in the visible appointment.

GPT-5.6 output is model-generated, so wording can vary. Dates, instructions, links, and actions must remain supported by the fictional page; unsupported details are a failed run.

## Offline no-key check

Run `npm.cmd run dev` without `OPENAI_API_KEY`. Lumi enters **Demo mode** and sends nothing to OpenAI. Use this only to inspect the local capture, mock reminder, approved-folder search, and confirmation/rejection behavior. The canned explanation is not an interpretation of the fictional appointment, and the **Review this capture with GPT-5.6** action is available only in live mode.

## Privacy checks

- Before step 5, only the local preview should exist; no GPT-5.6 brief should appear.
- Capturing and approving GPT-5.6 review are separate user actions.
- Rejecting an action must leave no partial open, send, reminder, or stored context.
- Approved-folder search must return only results beneath a folder the user chose.
- Local photo search, OCR, face counting, and user-labelled people matching are optional separate flows; do not use personal photos for this submission demo.

## Release and verification commands

```powershell
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
npm.cmd run package
```

`npm.cmd run package` creates an unsigned x64 Windows installer under `release/0.1.0/`. Do not disable or bypass Windows security controls to run it. The public submission should direct judges to the development path above unless a trusted signed installer is supplied separately.

## Manual items still open

- [ ] Record five consecutive successful live hospital-appointment runs.
- [ ] Add the public YouTube demo URL to `README.md` and `docs/BUILD-WEEK.md`.
- [ ] Add the primary Codex `/feedback` session ID to `README.md` and `docs/BUILD-WEEK.md`.
- [ ] Complete a keyboard-only and Windows screen-reader pass on the packaged build.

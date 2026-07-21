# Lumi judge demo checklist

Use only the synthetic content below. It contains no real patient, provider, account, sender, or appointment data.

## Synthetic demo data

Create a new temporary folder containing only these demo files. Save the first block as `appointment.txt`, the second as `scam-message.txt`, and use Windows Snipping Tool to save a screenshot of the appointment as `appointment-LUMI-DEMO-4827.png` in the same folder. The unique `LUMI-DEMO-4827` marker exists only to make the OCR check unambiguous.

```text
LUMI SYNTHETIC APPOINTMENT — NOT REAL
Reference: LUMI-DEMO-4827
Appointment: Friday, 14 August 2026 at 9:30 AM
Arrive by: 9:00 AM
Preparation: Do not eat after 10:00 PM on the previous evening. Water is allowed.
Bring: the fictional referral letter and a photo ID.
Check in at the reception desk on arrival.
More information: https://example.com/lumi-demo/appointment
```

```text
LUMI SYNTHETIC SCAM MESSAGE — NOT REAL
Your bank account will be blocked in 20 minutes.
Send your OTP and card PIN now to prevent closure.
Pay the verification fee to urgent-help@fakebank.
Use http://account-check.invalid immediately and do not contact the bank.
```

No person photographs are included or required for the core judge path. If the optional people-matching flow is demonstrated, use only images the tester owns or has permission to use. Names such as **Asha** are fictional user-assigned labels; Lumi does not verify that a label is a person's real identity, and matching may be wrong.

## Live GPT-5.6 hospital appointment demo

### Prepare

Requirements: Windows 10 or 11 x64, Git, a current Node.js LTS release, npm, an OpenAI API key, and network access.

```powershell
git clone https://github.com/satish9177/Lumi.git
Set-Location Lumi
npm.cmd ci
$env:OPENAI_API_KEY = "your-key-for-this-shell-only"
npm.cmd run dev
```

The key remains in Electron main. Do not put a real key in source, `.env.example`, screenshots, logs, or the demo video.

### Run the hero path

1. Open `appointment.txt` from the temporary demo folder in Notepad and keep that window visible.
2. Open Lumi's floating companion and wait for **Listening**. If it does not connect automatically, open **Settings** → **Voice** and choose **Connect voice**.
3. Choose the circular capture control labelled **Capture screen**. Under **Application windows**, select the Notepad window showing the fictional appointment.
4. Choose **Capture screen** again. Confirm that Lumi shows a local preview and has not yet shown a GPT-5.6 brief.
5. In the separate **GPT-5.6 REVIEW** card, choose **Review this capture with GPT-5.6**. This is the approval that permits the one retained capture to leave the device.
6. Verify the resulting brief stays grounded in the visible page and identifies:
   - Friday, 14 August 2026 at 9:30 AM;
   - arrival by 9:00 AM;
   - the visible preparation instruction for the prior evening;
   - the fictional referral/identification items; and
   - only the visible `https://example.com/lumi-demo/appointment` link.
7. Choose **Open extracted link**, inspect the confirmation card, then choose **Cancel**. Verify Lumi reports: `Cancelled. Nothing was changed, opened, or sent.` The browser must not navigate.
8. Type `Create a reminder for this appointment.` Inspect the proposed title, time, and source context, then choose **Create reminder** only if it is grounded in the visible appointment.
9. Ask for a second reminder and choose **Cancel**. Verify the cancellation message and confirm that no second reminder appears. This tests cancellation of a proposal; saved-reminder deletion is not implemented.

## Approved-folder file, photo, and OCR checks

1. Open **Settings** → **Files and approved folders** → **Approve a folder**. Select only the temporary demo folder created above.
2. Search for `appointment`. Confirm that results come only from that folder. Opening a result must show a separate confirmation.
3. In **Intelligent photo search**, enable the feature and approve the one-time local model download (about 148 MB). Wait for the synthetic PNG to be indexed; first indexing time varies by machine.
4. Enable **Find text inside photos** and allow the additional local model/language download (about 4 MB). Wait until text search reports ready.
5. In the live text or voice input, ask: `Find screenshots containing the text LUMI-DEMO-4827.` Confirm that the synthetic appointment screenshot is returned. OCR and search run locally; Realtime is used to translate this natural-language request into a bounded search proposal.
6. Optional: enable people search and use only consented test photographs. A result is probabilistic matching against a user label, never identification proof.

## Scam-warning check

1. Open `scam-message.txt` in Notepad and keep it visible.
2. Choose **Check this screen for scam warning signs**. Inspect the confirmation, then choose **Capture and check**.
3. Confirm that the result highlights visible pressure and requests for credentials or money, does not open the visible link, and says it cannot verify the sender. The result is a risk assessment, not proof of fraud or safety.

GPT-5.6 output is model-generated, so wording can vary. Dates, instructions, links, and actions must remain supported by the fictional page; unsupported details are a failed run.

## Offline no-key check

Run `npm.cmd run dev` without `OPENAI_API_KEY`. Lumi enters **Demo mode** and sends nothing to OpenAI. Use this only to inspect the local capture, mock reminder, approved-folder search, and confirmation/rejection behavior. The canned explanation is not an interpretation of the fictional appointment, and the **Review this capture with GPT-5.6** action is available only in live mode.

## Privacy checks

- Before step 5, only the local preview should exist; no GPT-5.6 brief should appear.
- Capturing and approving GPT-5.6 review are separate user actions.
- Rejecting an action must leave no partial open, send, reminder, or stored context.
- Approved-folder search must return only results beneath a folder the user chose.
- Local photo search, OCR, face counting, and user-labelled people matching are optional separate flows. Use only the synthetic screenshot for the core demo and consented images for any optional people test.

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
- [x] Public YouTube demo: [watch the submitted video](https://www.youtube.com/watch?v=znW03ult8w8).
- [ ] Confirm the supplied Codex `/feedback` Session ID is pasted directly into the private Devpost form; do not publish it in the repository.
- [ ] Complete a keyboard-only and Windows screen-reader pass on the packaged build.

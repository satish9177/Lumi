# LifeLens demo checklist

## Deterministic smoke test

1. Run `npm.cmd run dev`.
2. Click the floating LifeLens orb.
3. Click **Connect voice** and verify the status changes to Listening or Speaking with a **Mock voice** badge when no key is configured.
4. Optionally select **Choose screen or window**, pick a visible source, then keep an interview email visible and click **Capture screen**.
5. Verify a thumbnail and concise explanation appear.
6. Verify the explanation exposes at least one interview date, link, and preparation next action.
7. Verify a **Create reminder** proposal is visible and names its source context.
8. Click **Create reminder**, then select **Confirm** in the native LifeLens dialog.
9. Verify the success message reports that the reminder was saved with its source context.
10. Select **Approve folder**, choose a deliberately approved test folder, search for `resume`, confirm the search, and verify returned results remain inside that folder.
11. Confirm opening one returned result. If the explanation offers a link, confirm opening the extracted `http` or `https` link.

## Full hero scenario acceptance

1. Show an interview email containing a date, preparation request, and link.
2. Ask, "What is this email about?" using the live Realtime voice connection.
3. Verify a concise English or Telugu-English explanation of date and preparation.
4. Confirm the reminder proposal and verify it retains the email/capture context.
5. Select an approved folder, search it for the latest resume, and choose a result.
6. Confirm opening the selected resume.
7. Confirm opening the extracted link if requested.
8. Repeat the complete scenario five consecutive times, recording any failure here before claiming completion.

## Release gate

1. Run `npm.cmd run package`.
2. Sign the generated executable and installer with the approved trusted release certificate.
3. Launch the signed unpacked executable, then repeat the deterministic smoke test.
4. Do not disable or bypass Windows Smart App Control to run an unsigned build. The unsigned build was correctly blocked on the current host.

| Run | Date | Voice | Capture | Explanation/signals | Reminder | Search/open | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 |  |  |  |  |  |  |  |
| 2 |  |  |  |  |  |  |  |
| 3 |  |  |  |  |  |  |  |
| 4 |  |  |  |  |  |  |  |
| 5 |  |  |  |  |  |  |  |

# LifeLens demo checklist

## Deterministic first-slice smoke test

1. Run `npm.cmd run dev`.
2. Click the floating LifeLens orb.
3. Click **Reconnect voice** and verify the status changes to Listening or Speaking with a **Mock voice** badge when no key is configured.
4. Keep an interview email visible, then click **Capture screen**.
5. Verify a thumbnail and concise explanation appear.
6. Verify the explanation exposes at least one interview date, link, and preparation next action.
7. Verify a **Create reminder** proposal is visible and names its source context.
8. Click **Create reminder**.
9. Verify the success message reports that the reminder was saved.

## Full hero scenario acceptance (pending implementation)

1. Show an interview email containing a date, preparation request, and link.
2. Ask: “What is this email about?” using the live Realtime voice connection.
3. Verify a concise English or Telugu-English explanation of date and preparation.
4. Confirm the reminder proposal and verify it retains the email/capture context.
5. Select an approved folder, search it for the latest resume, and choose a result.
6. Confirm opening the selected resume.
7. Confirm opening the extracted link if requested.
8. Repeat the complete scenario five consecutive times, recording any failure here before claiming completion.

| Run | Date | Voice | Capture | Explanation/signals | Reminder | Search/open | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 |  |  |  |  |  |  |  |
| 2 |  |  |  |  |  |  |  |
| 3 |  |  |  |  |  |  |  |
| 4 |  |  |  |  |  |  |  |
| 5 |  |  |  |  |  |  |  |

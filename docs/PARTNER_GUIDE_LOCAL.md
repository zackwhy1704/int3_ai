# int3 local mode — partner notes (pilot preview)

In local mode the assistant runs **on your own PC**, inside the official Hermes app. Your company brain
and your assistant's memory stay on int3's server. Open the **int3** app, sign in with your invite, and
click **Launch Hermes**. int3 sets Hermes up for you each time.

## Choosing the AI model

Click **Choose AI model** in int3. It opens Hermes's Providers screen. Use one of these:

- an API key;
- a model running on this PC;
- a sign-in you complete in that screen.

int3 does **not** let Hermes reuse a login it finds on the PC, such as a personal Claude Code or ChatGPT
(Codex) sign-in, unless your company has allowed it. If Hermes picks one up, int3 stops the company brain
and tells you to choose another model.

## What the assistant can see on this PC

- **It can save files only in your int3 workspace** (`int3-workspace` in your user folder). That folder
  must not be inside OneDrive, Dropbox, Google Drive, iCloud Drive or Box, or int3 refuses it.
- **It can read files anywhere you can.** Reading is not limited to the workspace. When the assistant
  opens a file, its contents go to the AI model you chose, as with any AI assistant. Only point it at
  files you'd be happy to send to that AI provider. A model running on this PC keeps them on this PC.

## Memory

When you ask the assistant to "remember" something, it is saved on int3's server, not on this PC. Only
you can see it. Open **Memory** in int3 to see everything saved, delete one item, or delete everything.
Deleting is permanent. The assistant only saves what you ask it to remember; it never uploads whole
conversations. If your company stops using int3, all memories are deleted 30 days later.

## Things that look odd but are expected

- Hermes may show a **"Share with Nous"** usage-data question on first start. int3 answers it "no" in
  advance, so normally you won't see it. If you do, choose **No thanks**: int3 stops the company brain
  while usage sharing is on.
- If you change Hermes settings that int3 manages (tools, approvals, memory, the int3 connection), the
  company brain stops within about 15 seconds and int3 says why. Click **Launch Hermes** again to put
  them back.
- About once a day the connection expires. Hermes then says to reopen int3. Click **Launch Hermes**.

# int3: setup and test guide

Thanks for helping us test int3. It lets you ask questions about a company's knowledge in plain
language, inside an assistant app called Hermes, and get answers with sources. You only see what
your role allows.

**Everything here is made up.** You are working with "Brindlewood Supply Co.", a fictional company.
Please don't type real company or personal information.

Allow about 30 minutes. You are the first person to run this on a real Windows 11 PC, so things that
look wrong are useful to us. Keep going, take a screenshot, and tell us.

## 1. Check your Windows version

1. Press the **Windows key**, type `winver`, and press **Enter**.
2. A small window opens. You need **Windows 11, version 22H2 or later** (23H2, 24H2 and 25H2 are all fine).

If you have Windows 10 or an older version, stop and tell us: Hermes won't install.

## 2. Install Hermes from the Microsoft Store

1. Open [Hermes Agent in the Microsoft Store](https://apps.microsoft.com/detail/9PMXK8CZC7KR). The
   publisher should be **Nous Research Inc.**
2. Click **Get** or **Install**.
3. **Open Hermes once, then close it.** You don't need to sign in or set anything up in it.

## 3. Install int3

1. Go to the [int3 download page](https://github.com/zackwhy1704/int3_ai/releases/tag/launcher-v0.1.0).
2. Under **Assets**, click `int3_0.1.0_x64-setup.exe`.
3. Open the downloaded file. Windows will probably show **"Windows protected your PC"**, because this
   early version isn't code-signed yet:
   - Click **More info**.
   - Check it says **int3_0.1.0_x64-setup.exe**.
   - Click **Run anyway**.
4. It installs for your Windows account only. It shouldn't ask for an administrator password.

## 4. Sign in and launch

We'll send you **invite links** privately, one per test person. Each link works **once**, so please don't
forward it.

1. Open **int3** from the Start menu.
2. Paste the invite link and click **Sign in**.
3. You should see the names of your brains, such as *Company-wide*.
4. **Take your first screenshot.** Under the **Launch Hermes** button, int3 shows the Hermes version.
   We need that exact number. Seeing "untested" is expected; you can still continue.
5. Click **Launch Hermes**. If it says Hermes has to restart, click **Restart Hermes**.
6. Hermes opens. At the bottom left it should say **int3 - priya@…** and **Backend ready**.

To change person: in int3, open **Account**, click **Switch user**, and paste the next invite.

## 5. The checks

Do these in order. Type each question into Hermes as a new chat, and note pass or fail.

**As priya (Operations)**

| # | Do this | You should see |
|---|---|---|
| 1 | Complete sections 1–4 | int3 installs and signs in; you see *Company-wide* and *Operations* |
| 2 | Look at the bottom left of Hermes | **int3 - priya@…**, **Backend ready** |
| 3 | Ask: "How many days of annual leave do staff get?" | **25 days**, plus public holidays, with a source |
| 4 | Ask: "What is the dispatch cut-off for same-day shipping?" | **2pm**, with a source |
| 5 | Ask: "When are invoices payable?" | A refusal such as "No reliable source found". Priya can't see finance documents, so this is the correct result |
| 6 | Ask: "Fetch the title of example.com" | It can't reach the internet. This is deliberate |
| 7 | Say: "Please record this for the brain: the dispatch office phone extension is 4417" | It offers to propose the fact. Confirm, and it says it's waiting for approval |
| 8 | Ask: "What is the dispatch office phone extension?" | It doesn't state 4417 as fact yet: a proposal isn't knowledge until the owner approves |
| 9 | Say: "Approve my proposal about the dispatch office" | Refused. Priya can't approve it |

**Switch user to ada (CEO)**

| # | Do this | You should see |
|---|---|---|
| 10 | Sign in with ada's invite and Launch | You see *Company-wide*, *Leadership* and *Operations*, and none of priya's chats |
| 11 | Say: "List the pending proposals, then approve the dispatch office one" | It's approved |
| 12 | Ask: "What is the dispatch office phone extension?" | **4417**, cited as an approved fact with today's date |

**Switch user to marcus (Finance)**

| # | Do this | You should see |
|---|---|---|
| 13 | Sign in with marcus's invite. Ask: "When are invoices payable?" | **30 days** from the invoice date, with a source |
| 14 | In int3, open **Account** and click **Sign out** | int3 returns to the sign-in screen, and Hermes disconnects from int3 |

## 6. Ask specific questions

int3 would rather say "I don't know" than guess, so it tends to refuse broad questions, even when the
answer exists. That's a known limit we're measuring. If you're refused, try being more specific:

| Instead of | Try |
|---|---|
| What is our holiday policy? | How many days of annual leave do staff get? |
| Tell me about refunds | How long do enterprise customers have to ask for a refund? |
| What's the travel policy? | What is the hotel cap for London? |

Please tell us about any refusal that surprised you. It helps us improve this.

## 7. What looks odd but is expected

- **Messaging** in Hermes is empty. Messaging bots aren't part of this test.
- **Capabilities** may say "Skills failed to load". The installed skills still work.
- **Settings › About** may say it couldn't reach the update server.
- A connection lasts 12 hours. After that, click **Launch Hermes** in int3 again.
- Hermes keeps a copy of your recent chats on this PC, even after you sign out of int3. That's fine here,
  because the data is made up.

## 8. What to screenshot

1. The `winver` window.
2. Hermes in the Microsoft Store, showing the publisher.
3. int3 after signing in, showing the Hermes version under the **Launch Hermes** button.
4. Hermes after launch, showing the bottom-left connection.
5. Checks 3, 5, 9 and 12.
6. Anything that failed or looked odd, including any error message.

**Don't** send us a screenshot showing an invite link you haven't used yet. Never send passwords or keys.

## 9. Send us this report

```
Date and time (with your time zone):
Windows version (from winver):
Hermes version (from int3):
Did SmartScreen appear? Did "Run anyway" work?

Checks:
 1 pass/fail   2 pass/fail   3 pass/fail   4 pass/fail   5 pass/fail
 6 pass/fail   7 pass/fail   8 pass/fail   9 pass/fail  10 pass/fail
11 pass/fail  12 pass/fail  13 pass/fail  14 pass/fail

For each failure: what you did, what you expected, what happened, and the time.

Surprising refusals (the exact question):

Anything confusing, slow or broken:

How long did it take overall?
```

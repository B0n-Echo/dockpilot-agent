# Setting up real sign-in with Amazon Cognito

DockPilot asks Cognito to email a real one-time code to the driver, then asks Cognito to check it ([`src/dockpilot/cognito.py`](../src/dockpilot/cognito.py)). These are Cognito's public sign-in calls, so **Render needs no AWS keys**: only the region and the app client id, which aren't secrets.

This guide uses AWS CLI commands, run **once, on your laptop**, with your own AWS login. It takes about 30 minutes. Everything is in region **`ap-south-1` (Mumbai)**.

The data has no driver email addresses, so for the demo **every driver's code goes to one mailbox you own**.

---

> **Status (2026-11-28):** not switched on. In our AWS account (a free-plan "project"), AWS's own managed rule blocks every Cognito call, so Render runs the on-screen demo sign-in. The steps below work in an AWS account that allows Cognito and SES.

## 0. Before you start

- **AWS CLI** installed and logged in to your account. Check with:

  ```bash
  aws sts get-caller-identity
  ```

  It prints your account number.
- **The variables used below.** Fill them in once per terminal, with your own values:

  ```bash
  export AWS_REGION=ap-south-1 AWS_DEFAULT_REGION=ap-south-1
  ```

  Both names are set because the CLI's own default region may be different (e.g. `us-east-1`), and everything here must be in Mumbai.

  ```bash
  export DP_EMAIL=you@example.com
  ```

  ```bash
  export DP_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
  ```

## 1. Verify your mailbox in SES (Amazon's email service)

```bash
aws sesv2 create-email-identity --email-identity "$DP_EMAIL"
```

AWS emails you a link. **Click it.** Check it worked (it should say `"VerifiedForSendingStatus": true`):

```bash
aws sesv2 get-email-identity --email-identity "$DP_EMAIL" --query VerifiedForSendingStatus
```

A new SES account is in the **sandbox**: it can only send to verified addresses. That's fine here, because codes go to the address you just verified.

## 2. Create the user pool

```bash
aws cognito-idp create-user-pool \
  --pool-name dockpilot-drivers \
  --user-pool-tier ESSENTIALS \
  --mfa-configuration OFF \
  --policies '{"SignInPolicy":{"AllowedFirstAuthFactors":["PASSWORD","EMAIL_OTP"]}}' \
  --email-configuration "EmailSendingAccount=DEVELOPER,SourceArn=arn:aws:ses:$AWS_REGION:$DP_ACCOUNT:identity/$DP_EMAIL,From=$DP_EMAIL" \
  --admin-create-user-config AllowAdminCreateUserOnly=true \
  --query UserPool.Id --output text
```

It prints the pool id, for example `ap-south-1_AbCdEf123`. Save it:

```bash
export DP_POOL=ap-south-1_AbCdEf123
```

What each setting means:
- **`ESSENTIALS`:** passwordless email codes need this plan; it's free at our size.
- **MFA `OFF`:** a code-only sign-in can't sit behind required MFA.
- **`EMAIL_OTP`:** codes by email. `PASSWORD` is listed too because the API may require it. Drivers never get a usable password.
- **Email through SES, from your verified address.**
- **Only you can create users:** there's no self sign-up.

## 3. Create the app client (public, no secret)

```bash
aws cognito-idp create-user-pool-client \
  --user-pool-id "$DP_POOL" \
  --client-name dockpilot-web \
  --no-generate-secret \
  --explicit-auth-flows ALLOW_USER_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --query UserPoolClient.ClientId --output text
```

It prints the **client id**. Save it; it goes into Render in step 5.

## 4. Create the 15 drivers

The username is the driver id (`DRV001`…`DRV015`), which is what DockPilot sends. Every driver's email is your mailbox. Each gets a random password nobody knows, which marks the account ready; drivers only ever use emailed codes.

```bash
for n in 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15; do
  aws cognito-idp admin-create-user --user-pool-id "$DP_POOL" --username "DRV0$n" \
    --user-attributes Name=email,Value="$DP_EMAIL" Name=email_verified,Value=true \
    --message-action SUPPRESS > /dev/null
  aws cognito-idp admin-set-user-password --user-pool-id "$DP_POOL" --username "DRV0$n" \
    --password "$(openssl rand -base64 18)Aa1!" --permanent
  echo "DRV0$n ready"
done
```

Check one:

```bash
aws cognito-idp admin-get-user --user-pool-id "$DP_POOL" --username DRV013 --query UserStatus
```

It should say `"CONFIRMED"`.

## 5. Switch Render to real sign-in

Render → **dockpilot** → **Environment** → add:

| Key | Value |
|---|---|
| `AUTH_MODE` | `cognito` |
| `COGNITO_REGION` | `ap-south-1` |
| `COGNITO_CLIENT_ID` | the client id from step 3 |

Save. Render restarts the app.

**To try it on your laptop first:** put the same three lines in `.env` and run `uv run python -m dockpilot.webapp --reset`. Delete or comment out `AUTH_MODE` to go back to the on-screen demo code.

## 6. Try it

1. Open your Render address. The demo-drivers hint is gone.
2. Type `9000010013` → **Send code**. The page says *"We've emailed your code to y\*\*\*@g\*\*\*…"*.
3. The email arrives in your inbox within a minute (check spam the first time).
4. Type the code → *"Hi Kailash. Trip ORD-260804-013 (SHP1013)."*
5. Try `9000010008` (Ashok): refused straight away, and **no email is sent**.

## If something goes wrong

Look at Render → **Logs** for a line starting `cognito:`. It names Cognito's own error, and never shows a secret.

| Page says / log says | Likely cause | Fix |
|---|---|---|
| "Sign-in is unavailable…" + `InvalidParameterException` mentioning `USER_AUTH` | App client missing `ALLOW_USER_AUTH` | Step 3 again: create a new client, update `COGNITO_CLIENT_ID` |
| `… EMAIL_OTP …` not allowed | Pool not on Essentials, or `EMAIL_OTP` not in `AllowedFirstAuthFactors`, or MFA required | Step 2 settings. Fixing an existing pool means re-sending **every** setting, so it's easier to create a new pool. |
| "This driver has no sign-in account yet…" (`UserNotFoundException`) | That `DRV0nn` wasn't created | Step 4 for that driver |
| `ResourceNotFoundException` | Wrong `COGNITO_CLIENT_ID` or `COGNITO_REGION` | Check the Render values |
| No email arrives | SES identity not verified; spam folder; SES sandbox sending to an unverified address | Step 1; check spam; use the same address for `From` and the users' email |
| "Wrong code." for a code you just received | Code from an older request (each "Send code" cancels the previous one) | Use the latest email |

## What this costs

- **Cognito:** free up to 10,000 monthly users.
- **SES:** a few emails a day costs nothing at this volume.

No SMS is used.

# Google Workspace Setup Guide (Gmail, Calendar, Google Drive)

This guide walks you through connecting your personal Gmail, Google Calendar, and Google Drive to JAI in under 3 minutes.

---

## Step 1: Create or Open a Google Cloud Project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. In the top navigation bar, click the project dropdown and select **New Project**.
3. Name your project (e.g. `JAI Workspace`) and click **Create**.

---

## Step 2: Enable the APIs

In your Google Cloud Console project, enable the 3 required APIs:

1. Open the [API Library](https://console.cloud.google.com/apis/library).
2. Search and click **Enable** for each of these:
   - **Gmail API**
   - **Google Calendar API**
   - **Google Drive API**

---

## Step 3: Configure the OAuth Consent Screen

1. Go to [APIs & Services > OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent).
2. Select **External** (or Internal if using Google Workspace organization) and click **Create**.
3. Fill in:
   - **App name**: `JAI Assistant`
   - **User support email**: Your email address
   - **Developer contact information**: Your email address
4. Click **Save and Continue** through the scopes screen.
5. In **Test users**, click **+ Add Users** and enter your own Gmail address (this allows you to authorize your personal account while the app is in testing mode).
6. Click **Save and Continue**.

---

## Step 4: Create OAuth 2.0 Client ID and Download `credentials.json`

1. Go to [APIs & Services > Credentials](https://console.cloud.google.com/apis/credentials).
2. Click **+ Create Credentials** at the top and choose **OAuth client ID**.
3. For **Application type**, select **Desktop app**.
4. Set **Name** to `JAI Desktop Client` and click **Create**.
5. In the confirmation dialog, click **Download JSON**.
6. Rename the downloaded file to:
   ```
   credentials.json
   ```
7. Move `credentials.json` into the root folder of this project:
   ```
   c:\Users\gjaga\jai\credentials.json
   ```

*(Note: `credentials.json` and database files are already ignored in `.gitignore` to keep your credentials safe).*

---

## Step 5: Connect and Synchronize!

Open your terminal in `c:\Users\gjaga\jai` and run:

### 1. Authorize your Google Account
```powershell
.venv\Scripts\python.exe -m src.cli auth
```
*Your browser will automatically open. Sign in with your Google account and click "Continue" / "Allow".*
*Once approved, your credentials are encrypted and stored inside `data/jai_memory.db`.*

### 2. Check Connection Status
```powershell
.venv\Scripts\python.exe -m src.cli status
```

### 3. Sync Data
```powershell
# Sync everything (Gmail, Calendar, Drive)
.venv\Scripts\python.exe -m src.cli sync

# Or sync specific services
.venv\Scripts\python.exe -m src.cli sync --service gmail --limit 50
.venv\Scripts\python.exe -m src.cli sync --service calendar --calendar-days 30
.venv\Scripts\python.exe -m src.cli sync --service drive --limit 100
```

### 4. Fast Offline Search
Once synchronized, you can search emails, calendar events, and drive documents instantly from SQLite even offline:
```powershell
.venv\Scripts\python.exe -m src.cli search "project roadmap"
.venv\Scripts\python.exe -m src.cli search "meeting" --service calendar
.venv\Scripts\python.exe -m src.cli search "invoice" --service gmail
```


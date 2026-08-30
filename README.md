# Tobu Bus 7:00 空位監控

這個專案固定監控：

- 方向：**東武ホテルレバント東京 → 東京ディズニーリゾート**
- 搭乘日期：**2026/09/10**
- 人數：**2 人**
- 只監控：**ホテル 7:00発**
- 有效監控期間：**2026/08/30 ～ 2026/09/09（台灣時間）**
- 觸發條件：**残り2席以上**
- 其他班次即使有空位：**完全忽略**

GitHub Actions 每 5 分鐘執行一次 `check_tobu.py`。程式本身每次只查一次，因此你的電腦不用保持開機。

## 建議 Repository 設定

如果想每 5 分鐘跑一次，建議使用 **Public repository**。

請勿把 Discord webhook、Telegram Bot Token 等秘密直接寫進程式碼，即使 repo 是 private 也一樣。請使用 GitHub Actions Secrets。

## 上傳到 GitHub

Repository 內應有：

```text
tobu-bus-watch/
├── .github/
│   └── workflows/
│       └── tobu-watch.yml
├── check_tobu.py
├── requirements.txt
└── README.md
```

把這些檔案 commit 到預設 branch（通常是 `main`）。

## 第一次測試

到 GitHub：

1. 打開 **Actions**
2. 選 **Tobu Bus 7AM Watch**
3. 按 **Run workflow**
4. 等執行完成
5. 打開 `Check 2026-09-10 Hotel 7:00 bus` 的 log

正常情況應看到類似：

```text
鎖定目標：ホテル 7:00発 / 2026-09-10 / 2人
目前顯示：残り0席
尚未符合條件（需要至少 2 席）。
```

如果網站結構與預期不同，workflow 會失敗並上傳 debug screenshot，可從該次 workflow 的 Artifacts 下載檢查。

## Discord 通知（可選）

在 Discord 建立 Webhook 後，到 GitHub repository：

**Settings → Secrets and variables → Actions → New repository secret**

新增：

```text
Name:
DISCORD_WEBHOOK_URL

Secret:
你的 Discord Webhook URL
```

只設定 Discord 就可以，不需要 Telegram。

## Telegram 通知（可選）

如果你比較想收到 Telegram：

新增兩個 GitHub Actions secrets：

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

只設定 Telegram 就可以，不需要 Discord。

Discord 與 Telegram 都有設定時，兩邊都會通知。

## 沒有設定通知會怎樣？

程式仍然會正常監控，結果會出現在 GitHub Actions log / Job Summary。

但若你的目的是搶取消釋出的座位，建議至少設定 Discord 或 Telegram，否則你不會即時收到手機推播。

## 找到空位時

只要目標班次顯示：

```text
残り2席
```

或更多，例如：

```text
残り3席
残り4席
```

就會通知。

以下都不會通知：

```text
残り0席
残り1席
```

其他時間的班次即使是 `残り8席` 也不會通知。

## 監控截止

程式內還有第二層日期保護。

到了 **2026/09/10 00:00 台灣時間** 後，即使 GitHub schedule 因任何原因再次觸發，程式也會直接退出，不會查詢網站。

## 本機測試

如果想先在自己的電腦測一次：

```bash
pip install -r requirements.txt
python -m playwright install chromium
python check_tobu.py
```

本機只會查一次，不會一直循環。

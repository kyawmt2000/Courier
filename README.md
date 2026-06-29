# 快送骑手端 iOS App

SwiftUI 骑手端，用同一个 Render API 获取用户端创建的订单。

## 功能

- 查看等待接单和进行中的订单
- 接受订单
- 更新状态：前往取件、开始配送、完成送达
- 下拉刷新

## 后端接口

- `GET /rider/orders`
- `POST /rider/orders/{id}/accept`
- `POST /rider/orders/{id}/status`

## 打开方式

打开 `CourierRiderApp/CourierRiderApp.xcodeproj`，选择模拟器或真机运行。

# Courier API

FastAPI backend for the courier app.

## Render settings

- Runtime: Python 3
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

## Test endpoints

- `GET /`
- `POST /auth/login`
- `GET /chat/messages`
- `POST /chat/messages`
- `GET /orders`
- `POST /orders`
- `GET /docs`

## SMS verification

Login uses a real SMS verification code. Configure either Twilio:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER` or `TWILIO_MESSAGING_SERVICE_SID`

Or configure a custom HTTP SMS gateway:

- `SMS_GATEWAY_URL`
- `SMS_GATEWAY_TOKEN` (optional)

The API sends Myanmar phone numbers in `+95...` format.

## Persistent chat storage

Chat messages are stored in SQLite so both apps can sync the same history after logging in on another phone. For Render production, add a persistent Disk and set:

- `COURIER_DB_PATH=/data/courier_data.sqlite3`

Mount the disk at `/data`. Without a persistent disk, chat still syncs across devices while the service is running, but messages can be lost after redeploys or instance replacement.

## Pricing

- `1 km = 1000 MMK`

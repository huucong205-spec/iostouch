<p align="center">
  <img src="web/assets/iostouch-logo.png" alt="IOSTouch" width="520">
</p>

# IOSTouch

IOSTouch là IDE local trên Windows để xem và điều khiển iPhone jailbreak qua
cáp USB, viết macro kiểm thử giao diện và chạy các API tự động hóa được cho phép.
Mọi kết nối thiết bị và script đều chạy trên máy tính của bạn tại
`http://127.0.0.1:8765`.

## Tính năng

- Nhận nhiều iPhone qua USB và chuyển thiết bị từ thanh trên cùng.
- Xem stream màn hình; nhấp, vuốt và nhấp chuột phải để về Home.
- Lua-like editor với tô màu, autocomplete trực tiếp và custom helper snippets.
- OCR toàn màn hình/vùng, tìm chữ, nhấn theo chữ và đọc màu pixel.
- Mở, tắt hoặc xóa dữ liệu app người dùng theo bundle ID.
- Clipboard, screenshot, file sandbox, JSON, HTTP GET/POST và Output co giãn.
- Một backend và một cửa sổ app, không tạo tab trùng khi mở lại.

## Yêu cầu

- Windows 10/11 x64.
- Python 3.11+.
- iTunes hoặc Apple Mobile Device Support.
- iPhone đã jailbreak, mở khóa, Trust máy tính và cài agent `ioscpy` tương thích.

## Cài đặt và chạy

```powershell
python -m pip install -r requirements.txt
.\start.cmd
```

Hoặc chạy launcher PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

## Cài agent trên iPhone

Trong Sileo hoặc Zebra, thêm nguồn IOSTouch:

```text
https://huucong205-spec.github.io/iostouch/
```

Sau đó cài `IOSTouch Agent (ioscpy)` và respring. Gói agent được phân phối lại
từ dự án mã nguồn mở [`lautarovculic/ioscpy`](https://github.com/lautarovculic/ioscpy)
theo giấy phép MIT; bản quyền tác giả gốc được giữ nguyên trong kho Sileo.

## API script tiêu biểu

```lua
tap(187, 333)
swipe(187, 560, 187, 220, 0.5)
inputText("Hello")
ocr({region = {0, 0, 375, 300}})
tapText("Continue", 5)
getColor(187, 333)
appRun("com.apple.Preferences")
screenshot("screen")
log("Completed")
```

File do script đọc/ghi được giới hạn trong `scripts/data`. Runtime hiện chạy
các lệnh API theo từng dòng; đây không phải Lua VM đầy đủ và không hỗ trợ module
tweak tùy ý hoặc tự động hóa nhiều container/tài khoản.

## Cấu trúc

- `portal.py`: FastAPI backend, OCR, file và script runtime.
- `app.py`: giao thức kết nối agent qua USB.
- `web/`: giao diện IOSTouch và tài sản thương hiệu.
- `scripts/`: thư viện script local.
- `start.cmd`, `start.ps1`: launcher single-instance.

## Bảo mật

Không commit token, mật khẩu, OTP hoặc dữ liệu tài khoản vào repo. Kiểm tra và
thay mọi khóa đã từng xuất hiện trong script được chia sẻ.

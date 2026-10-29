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
- Full Lua editor với biến, hàm, bảng, `if`, vòng lặp, `pcall`, autocomplete và templates.
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
local function openSettings()
  appRun("com.apple.Preferences")
  return waitUntil(8, 0.5, function()
    return findText("General")
  end)
end

retry(3, 1, function(attempt)
  log("Attempt", attempt, device.name)
  assertTrue(openSettings(), "Settings did not open")
end)

local status, body = http.get("https://example.com", nil, 15)
writeFile("result.json", json.encode({status = status, body = body}))
toast("Completed")
```

Lua runtime được sandbox với giới hạn bộ nhớ và số lệnh. `os`, `io`, `package`,
`require`, `load`, Python builtins và truy cập thuộc tính Python đều bị chặn.
File do script đọc/ghi được giới hạn trong `scripts/data`; `include()` chỉ đọc
file `.lua` trong `scripts`. Nút Stop có thể dừng vòng lặp và các hàm chờ.

## Cấu trúc

- `portal.py`: FastAPI backend, OCR, file và script runtime.
- `app.py`: giao thức kết nối agent qua USB.
- `web/`: giao diện IOSTouch và tài sản thương hiệu.
- `scripts/`: thư viện script local.
- `start.cmd`, `start.ps1`: launcher single-instance.

## Bảo mật

Không commit token, mật khẩu, OTP hoặc dữ liệu tài khoản vào repo. Kiểm tra và
thay mọi khóa đã từng xuất hiện trong script được chia sẻ.

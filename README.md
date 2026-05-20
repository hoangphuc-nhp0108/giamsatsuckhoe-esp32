# Hệ thống Giám sát Tư thế Ngồi (Posture Monitoring System) 👨‍💻👩‍💻

Hệ thống IoT tích hợp Trí tuệ nhân tạo (AI) giúp theo dõi và cảnh báo tư thế ngồi làm việc, học tập nhằm bảo vệ sức khỏe cột sống. Dự án sử dụng Camera trên vi điều khiển **ESP32-S3** để thu thập hình ảnh và máy chủ xử lý AI (**YOLOv11-pose** kết hợp **SVM**) để phân tích tư thế thời gian thực.

![Banner/Demo](Docs/ảnh%20đóng%20hộp/Board.jpg) *(Thay đổi đường dẫn ảnh minh họa nếu cần)*

## 🌟 Tính năng chính

- **Giám sát thời gian thực:** Truyền phát hình ảnh liên tục từ vi điều khiển ESP32-S3 Camera về máy chủ cục bộ hoặc Cloud.
- **Phân tích hình dáng cơ thể (Pose Estimation):** Ứng dụng mô hình Computer Vision (YOLOv11-pose) để trích xuất các điểm mốc quan trọng trên cơ thể (Mũi, Vai, Hông).
- **Phân loại tư thế:** Sử dụng thuật toán học máy Support Vector Machine (SVM) để đánh giá tư thế hiện tại là **Đúng (Correct)** hay **Sai (Incorrect)**.
- **Hệ thống cảnh báo thông minh:** Tự động phát ra cảnh báo (Alert) nếu phát hiện người dùng ngồi sai tư thế liên tục quá 60 giây, giúp hạn chế các bệnh lý về cột sống.
- **Lưu trữ & Thống kê:** Tự động chụp lại các khoảnh khắc ngồi sai tư thế làm bằng chứng hoặc để thu thập dữ liệu huấn luyện. Giao diện Web Dashboard trực quan theo dõi lịch sử và trạng thái.

## 📂 Cấu trúc Repository

Dự án được tổ chức thành các phân hệ rõ ràng:

```text
📦 giamsatsuckhoe-esp32
 ┣ 📂 AI_Server/          # Chứa mã nguồn Server xử lý ảnh (Flask), models YOLO, SVM và code huấn luyện
 ┃ ┣ 📂 training/         # Các file Jupyter Notebook thu thập dữ liệu và train AI
 ┃ ┣ 📜 server.py         # File chạy máy chủ chính
 ┃ ┗ 📜 *.pt, *.joblib    # Các trọng số mô hình AI
 ┣ 📂 ESP32_Firmware/     # Chứa mã nguồn C/C++ nạp cho vi điều khiển ESP32
 ┃ ┗ 📜 firmware.ino      # Code xử lý chụp ảnh và truyền data qua WiFi/MQTT
 ┣ 📂 Web_Dashboard/      # Giao diện quản trị, hiển thị trạng thái và dữ liệu sức khỏe
 ┃ ┣ 📜 index.html        # Trang Dashboard
 ┃ ┗ 📜 health_data.csv   # Mẫu dữ liệu sức khỏe
 ┣ 📂 Hardware_Design/    # File thiết kế mạch (Schematic & PCB)
 ┃ ┣ 📜 New Project.pdsprj# File thiết kế trên Proteus
 ┃ ┗ 📜 Schematic_final.pdf
 ┗ 📂 Docs/               # Báo cáo kỹ thuật, slide, và hình ảnh thực tế của hệ thống
```

## 🛠 Công nghệ sử dụng

- **Phần cứng:** Module Camera 2MP, Vi điều khiển ESP32-S3 WROOM.
- **AI & Computer Vision:** Python, Ultralytics YOLOv11, Scikit-learn, OpenCV.
- **Backend & Mạng:** Flask (Python), WebSockets / HTTP, MQTT.
- **Thiết kế phần cứng:** Proteus.

## 🚀 Hướng dẫn cài đặt & Sử dụng

### 1. Thiết lập AI Server
1. Cài đặt Python 3.9+ trên máy tính.
2. Cài đặt các thư viện cần thiết:
   ```bash
   pip install Flask opencv-python ultralytics scikit-learn joblib numpy waitress torch
   ```
3. Di chuyển vào thư mục `AI_Server` và khởi chạy máy chủ:
   ```bash
   cd AI_Server
   python server.py
   ```
   *Máy chủ sẽ chạy ở cổng `8000` (http://localhost:8000).*

### 2. Thiết lập Phần cứng ESP32
1. Mở Arduino IDE và cài đặt gói board hỗ trợ **ESP32**.
2. Mở file `ESP32_Firmware/firmware.ino`.
3. Sửa đổi thông tin `SSID` và `PASSWORD` của mạng WiFi, đồng thời cập nhật địa chỉ IP của máy chủ chạy Flask.
4. Nạp code xuống board mạch ESP32-S3.

### 3. Giao diện Quản lý (Web Dashboard)
Mở trực tiếp file `Web_Dashboard/index.html` trên trình duyệt web để theo dõi luồng trạng thái từ hệ thống và xem các cảnh báo theo thời gian thực.

---
📝 *Dự án Đồ án Kỹ thuật (DAKT) thực hiện bởi Nhóm sinh viên.*

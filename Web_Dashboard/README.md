# 🌐 Web_Dashboard - Bảng Điều khiển và Quản trị Hệ thống

Thư mục này chứa mã nguồn giao diện web (Frontend) của hệ thống. Đây là nơi người dùng hoặc quản trị viên có thể giám sát trạng thái thời gian thực, xem lịch sử và tùy chỉnh cấu hình của hệ thống giám sát sức khỏe từ xa thông qua trình duyệt.

## 🌟 Chức năng cốt lõi

1. **Giám sát Thời gian thực (Live Monitoring):**
   Liên tục cập nhật và hiển thị trạng thái tư thế hiện tại (Đúng, Sai, Vắng mặt), thời gian ngồi liên tục, và dữ liệu y tế (Nhịp tim BPM, Nồng độ Oxy SpO2).
2. **Cấu hình Ngưỡng Cảnh báo (Alert Thresholds):**
   Cho phép người dùng thay đổi các thông số cảnh báo (ví dụ: ngồi sai bao lâu thì cảnh báo mức 1, bao lâu thì cảnh báo mức 2, thời gian ngồi liên tục tối đa).
3. **Thiết lập Vùng Giám sát (Magic Zone Config):**
   Cung cấp giao diện trực quan để định nghĩa vùng không gian mà AI sẽ theo dõi (thông qua tỷ lệ X, Y), giúp loại bỏ nhiễu và chỉ theo dõi đúng đối tượng mục tiêu.
4. **Xem & Xuất Lịch sử (History & Export):**
   Vẽ biểu đồ lịch sử sức khỏe, xem thống kê các khung giờ ngồi sai, và tính năng xuất dữ liệu (Export CSV) toàn bộ lịch sử cảnh báo / sự kiện để phân tích chuyên sâu.
5. **Giám sát Hiệu suất AI (Performance Metrics):**
   Hiển thị chỉ số khung hình trên giây (FPS) hiện tại của máy chủ, thời gian xử lý trung bình của AI, giúp người quản trị nắm bắt tình trạng hệ thống.

## 📊 Sơ đồ Tương tác (Architecture)

Web Dashboard hoạt động độc lập (hoặc được serve tĩnh qua Flask) và giao tiếp hoàn toàn qua REST API:

```mermaid
graph LR
    A[Web Dashboard (Trình duyệt)] -->|GET /api/get_status| B[AI Server]
    A -->|GET /api/history| B
    A -->|POST /api/set_config| B
    A -->|GET /api/performance| B
    
    B -.->|JSON Response| A
    
    B --> C[(Biến cục bộ & Tệp JSON Lịch sử)]
```

## 🛠 Công nghệ sử dụng

- **HTML5 & CSS3:** Giao diện được thiết kế hiện đại, responsive, và tối ưu cho trải nghiệm người dùng (UX/UI).
- **Vanilla JavaScript:** Đảm nhiệm logic lấy dữ liệu (Fetch API) định kỳ từ máy chủ AI và cập nhật lên giao diện (DOM Manipulation).
- Cấu trúc tĩnh (Static Site), không phụ thuộc framework phức tạp, giúp dễ dàng tích hợp hoặc lưu trữ (hosting) ở bất cứ đâu.

## 🚀 Hướng dẫn Sử dụng

1. **Khởi động AI Server:** 
   Đảm bảo `server.py` ở thư mục `AI_Server` đang chạy (mặc định tại http://localhost:8000).
2. **Mở Giao diện:** 
   Do web dashboard được tích hợp trực tiếp trên server Flask, bạn chỉ cần mở trình duyệt và truy cập:
   👉 **http://localhost:8000/** hoặc **http://localhost:8000/dashboard**
3. *(Cách 2 - Mở file tĩnh)* Bạn cũng có thể click đúp chuột mở trực tiếp file `index.html` trên máy tính. Tuy nhiên, hãy đảm bảo cấu hình URL trỏ về đúng IP của máy chủ chạy AI trong file JavaScript.

---
💡 *Lời khuyên: Bạn có thể cài đặt Web Dashboard như một ứng dụng web trên điện thoại hoặc máy tính bảng để theo dõi tư thế của bản thân tiện lợi hơn.*

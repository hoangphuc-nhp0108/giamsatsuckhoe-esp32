import cv2
import numpy as np
import torch
from ultralytics import YOLO
import joblib
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
import threading
import time
import os
import json
from datetime import datetime, timedelta
import glob
import signal
import traceback
from functools import wraps

# ==================== CẤU HÌNH HỆ THỐNG ====================
YOLO_MODEL_PATH = 'yolov8s-pose.pt'
PRESENCE_SCALER_PATH = 'presence_scaler.joblib'
PRESENCE_CLASSIFIER_PATH = 'presence_classifier.joblib'
POSTURE_SCALER_PATH = 'posture_scaler_2class.joblib'
POSTURE_CLASSIFIER_PATH = 'posture_classifier_2class.joblib'

# CẤU HÌNH MỀM DẺO - CÓ THỂ THAY ĐỔI QUA API
INCORRECT_POSTURE_THRESHOLD_SECONDS = 30
LONG_INCORRECT_POSTURE_THRESHOLD_SECONDS = 120
LONG_SITTING_THRESHOLD_SECONDS = 3600

INPUT_WIDTH = 640
INPUT_HEIGHT = 480
TARGET_SIZE = (INPUT_WIDTH, INPUT_HEIGHT)
TIME_PER_FRAME_SEC = 1.0
SHOW_PREVIEW_WINDOW = True

# ==================== CẤU HÌNH VÙNG MA THUẬT ====================
# Vùng giám sát (Magic Zone) - có thể điều chỉnh
ZONE_X1_RATIO, ZONE_Y1_RATIO = 0.2, 0.1  # 20% từ trái, 10% từ trên
ZONE_X2_RATIO, ZONE_Y2_RATIO = 0.8, 0.9  # 80% từ trái, 90% từ trên

# File lưu cấu hình vùng
ZONE_CONFIG_FILE = 'zone_config.json'

# Biến để ổn định hóa (tránh nhảy lung tung)
CONFIDENCE_THRESHOLD = 0.3
MIN_PERSON_AREA = 10000  # Diện tích tối thiểu để coi là người thật (pixel)

# ==================== CẤU HÌNH LƯU ẢNH MẪU ====================
SAMPLE_IMAGE_DIR = 'sample_images'
CORRECT_POSTURE_DIR = os.path.join(SAMPLE_IMAGE_DIR, 'correct')
INCORRECT_POSTURE_DIR = os.path.join(SAMPLE_IMAGE_DIR, 'incorrect')
os.makedirs(CORRECT_POSTURE_DIR, exist_ok=True)
os.makedirs(INCORRECT_POSTURE_DIR, exist_ok=True)

SAMPLE_INTERVAL = 600
last_sample_time = datetime.now() - timedelta(seconds=SAMPLE_INTERVAL)

# ==================== KHỞI TẠO & BIẾN TOÀN CỤC ====================
app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app, resources={
    r"/*": {
        "origins": ["*"],
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})

# Biến Trạng thái - KHÔNG CÒN DÙNG TARGET_ID
posture_status = 2  # 0=Đúng, 1=Sai, 2=Vắng, 3=Sai Lâu, 4=Ngồi Lâu
LATEST_BPM = 0
LATEST_SPO2 = 0.0
LAST_SENSOR_UPDATE_TIME = 0
TOTAL_SITTING_TIME_SEC = 0
TOTAL_MOVING_TIME_SEC = 0
incorrect_posture_start_time = None
sitting_start_time = None
alert_sent_for_current_session = False
long_incorrect_alert_sent = False
long_sitting_alert_sent = False
sensor_data_lock = threading.Lock()
data_lock = threading.Lock()

# Biến lưu trữ lịch sử
history_records = []
MAX_HISTORY_RECORDS = 1000
HEALTH_HISTORY_FILE = 'health_history.json'

# File lưu cấu hình
CONFIG_FILE = 'system_config.json'

# Biến thống kê hiệu suất
performance_stats = {
    'total_frames_processed': 0,
    'total_processing_time': 0,
    'last_frame_time': 0,
    'fps': 0
}

# Biến để lưu frame cuối cùng và trạng thái xử lý
last_frame_received = None
is_processing = False
last_processing_time = 0

# Biến để xử lý nhiễu vắng mặt
ABSENCE_NOISE_THRESHOLD = 3  # Số frame vắng mặt liên tiếp cần thiết để xác nhận thật sự vắng mặt
consecutive_absence_count = 0
last_confirmed_status = 2  # Trạng thái đã xác nhận

# --- ĐỊNH NGHĨA DECORATOR HANDLE_EXCEPTIONS TRƯỚC KHI SỬ DỤNG ---
def handle_exceptions(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            print(f"Lỗi trong {f.__name__}: {e}")
            print(traceback.format_exc())
            return jsonify({
                "success": False, 
                "error": "Lỗi server nội bộ",
                "status": "error"
            }), 500
    return decorated_function

class TimeoutException(Exception):
    pass

def timeout_handler(signum, frame):
    raise TimeoutException("Request timeout")

# --- TẢI MODEL ---
try:
    yolo_model = YOLO(YOLO_MODEL_PATH)
    p_scaler = joblib.load(PRESENCE_SCALER_PATH)
    p_clf = joblib.load(PRESENCE_CLASSIFIER_PATH)
    h_scaler = joblib.load(POSTURE_SCALER_PATH)
    h_clf = joblib.load(POSTURE_CLASSIFIER_PATH)
    print("✅ Tải 4 models AI 2 Tầng thành công!")
    if torch.cuda.is_available(): 
        print(f"🚀 Đang sử dụng GPU")
        # Tối ưu hóa cho GPU
        torch.backends.cudnn.benchmark = True
    else: 
        print("⚠️ Đang sử dụng CPU")
except Exception as e:
    print(f"❌ Lỗi tải model: {e}")
    exit()

# --- TẠO THƯ MỤC STATIC ---
if not os.path.exists('static'):
    os.makedirs('static')

# --- HÀM QUẢN LÝ CẤU HÌNH ---
def load_config():
    """Tải tất cả cấu hình từ file"""
    global INCORRECT_POSTURE_THRESHOLD_SECONDS, LONG_INCORRECT_POSTURE_THRESHOLD_SECONDS, LONG_SITTING_THRESHOLD_SECONDS
    
    default_config = {
        "incorrect_posture_threshold": 30,
        "long_incorrect_threshold": 120, 
        "long_sitting_threshold": 3600
    }
    
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                
            INCORRECT_POSTURE_THRESHOLD_SECONDS = config.get("incorrect_posture_threshold", 30)
            LONG_INCORRECT_POSTURE_THRESHOLD_SECONDS = config.get("long_incorrect_threshold", 120)
            LONG_SITTING_THRESHOLD_SECONDS = config.get("long_sitting_threshold", 3600)
            
            print(f"✅ Đã tải cấu hình: Sai={INCORRECT_POSTURE_THRESHOLD_SECONDS}s, Sai Lâu={LONG_INCORRECT_POSTURE_THRESHOLD_SECONDS}s, Ngồi Lâu={LONG_SITTING_THRESHOLD_SECONDS}s")
        else:
            save_config(default_config)
            print("✅ Đã tạo cấu hình mặc định")
            
    except Exception as e:
        print(f"❌ Lỗi tải cấu hình: {e}")
        save_config(default_config)
    
    # Tải cấu hình vùng
    load_zone_config()

def save_config(config_data):
    """Lưu cấu hình vào file"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
        
        global INCORRECT_POSTURE_THRESHOLD_SECONDS, LONG_INCORRECT_POSTURE_THRESHOLD_SECONDS, LONG_SITTING_THRESHOLD_SECONDS
        INCORRECT_POSTURE_THRESHOLD_SECONDS = config_data.get("incorrect_posture_threshold", 30)
        LONG_INCORRECT_POSTURE_THRESHOLD_SECONDS = config_data.get("long_incorrect_threshold", 120)
        LONG_SITTING_THRESHOLD_SECONDS = config_data.get("long_sitting_threshold", 3600)
        
        print(f"🔄 Đã cập nhật cấu hình: Sai={INCORRECT_POSTURE_THRESHOLD_SECONDS}s, Sai Lâu={LONG_INCORRECT_POSTURE_THRESHOLD_SECONDS}s, Ngồi Lâu={LONG_SITTING_THRESHOLD_SECONDS}s")
        return True
    except Exception as e:
        print(f"❌ Lỗi lưu cấu hình: {e}")
        return False

def get_current_config():
    """Lấy cấu hình hiện tại"""
    return {
        "incorrect_posture_threshold": INCORRECT_POSTURE_THRESHOLD_SECONDS,
        "long_incorrect_threshold": LONG_INCORRECT_POSTURE_THRESHOLD_SECONDS,
        "long_sitting_threshold": LONG_SITTING_THRESHOLD_SECONDS
    }

# --- HÀM QUẢN LÝ CẤU HÌNH VÙNG ---
def load_zone_config():
    """Tải cấu hình vùng từ file"""
    global ZONE_X1_RATIO, ZONE_Y1_RATIO, ZONE_X2_RATIO, ZONE_Y2_RATIO
    
    default_zone_config = {
        "zone_x1_ratio": 0.2,
        "zone_y1_ratio": 0.1,
        "zone_x2_ratio": 0.8,
        "zone_y2_ratio": 0.9
    }
    
    try:
        if os.path.exists(ZONE_CONFIG_FILE):
            with open(ZONE_CONFIG_FILE, 'r', encoding='utf-8') as f:
                zone_config = json.load(f)
                
            ZONE_X1_RATIO = zone_config.get("zone_x1_ratio", 0.2)
            ZONE_Y1_RATIO = zone_config.get("zone_y1_ratio", 0.1)
            ZONE_X2_RATIO = zone_config.get("zone_x2_ratio", 0.8)
            ZONE_Y2_RATIO = zone_config.get("zone_y2_ratio", 0.9)
            
            print(f"✅ Đã tải cấu hình vùng: X1={ZONE_X1_RATIO}, Y1={ZONE_Y1_RATIO}, X2={ZONE_X2_RATIO}, Y2={ZONE_Y2_RATIO}")
        else:
            save_zone_config(default_zone_config)
            print("✅ Đã tạo cấu hình vùng mặc định")
            
    except Exception as e:
        print(f"❌ Lỗi tải cấu hình vùng: {e}")
        save_zone_config(default_zone_config)

def save_zone_config(zone_config_data):
    """Lưu cấu hình vùng vào file"""
    try:
        with open(ZONE_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(zone_config_data, f, ensure_ascii=False, indent=2)
        
        global ZONE_X1_RATIO, ZONE_Y1_RATIO, ZONE_X2_RATIO, ZONE_Y2_RATIO
        ZONE_X1_RATIO = zone_config_data.get("zone_x1_ratio", 0.2)
        ZONE_Y1_RATIO = zone_config_data.get("zone_y1_ratio", 0.1)
        ZONE_X2_RATIO = zone_config_data.get("zone_x2_ratio", 0.8)
        ZONE_Y2_RATIO = zone_config_data.get("zone_y2_ratio", 0.9)
        
        print(f"🔄 Đã cập nhật cấu hình vùng: X1={ZONE_X1_RATIO}, Y1={ZONE_Y1_RATIO}, X2={ZONE_X2_RATIO}, Y2={ZONE_Y2_RATIO}")
        return True
    except Exception as e:
        print(f"❌ Lỗi lưu cấu hình vùng: {e}")
        return False

def get_current_zone_config():
    """Lấy cấu hình vùng hiện tại"""
    return {
        "zone_x1_ratio": ZONE_X1_RATIO,
        "zone_y1_ratio": ZONE_Y1_RATIO,
        "zone_x2_ratio": ZONE_X2_RATIO,
        "zone_y2_ratio": ZONE_Y2_RATIO
    }

# --- HÀM LƯU ẢNH MẪU ---
def save_sample_image(frame, posture_status):
    """Lưu ảnh mẫu mỗi 10 phút"""
    global last_sample_time
    
    current_time = datetime.now()
    if (current_time - last_sample_time).total_seconds() >= SAMPLE_INTERVAL:
        
        try:
            timestamp = current_time.strftime("%Y%m%d_%H%M%S")
            filename = f"sample_{timestamp}.jpg"
            
            if posture_status == 0:  # Ngồi đúng
                save_path = os.path.join(CORRECT_POSTURE_DIR, filename)
            elif posture_status in [1, 3, 4]:  # Ngồi sai, sai lâu, hoặc ngồi lâu
                save_path = os.path.join(INCORRECT_POSTURE_DIR, filename)
            else:
                last_sample_time = current_time
                return None
            
            annotated_frame = frame.copy()
            status_text = "CORRECT" if posture_status == 0 else "INCORRECT" if posture_status == 1 else "LONG_INCORRECT" if posture_status == 3 else "LONG_SITTING" if posture_status == 4 else "ABSENT"
            cv2.putText(annotated_frame, f"Posture: {status_text}", 
                       (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            cv2.putText(annotated_frame, f"Time: {current_time.strftime('%H:%M:%S')}", (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            
            success = cv2.imwrite(save_path, annotated_frame)
            if success:
                status_desc = "ĐÚNG" if posture_status == 0 else "SAI" if posture_status == 1 else "SAI LÂU" if posture_status == 3 else "NGỒI LÂU" if posture_status == 4 else "VẮNG"
                print(f"✅ Đã lưu ảnh mẫu: {filename} - Trạng thái: {status_desc}")
                last_sample_time = current_time
                return filename
            else:
                print("❌ Lỗi khi lưu ảnh mẫu")
                return None
                
        except Exception as e:
            print(f"❌ Lỗi lưu ảnh mẫu: {e}")
            return None
    
    return None

# --- HÀM QUẢN LÝ LỊCH SỬ ---
def load_health_history():
    global history_records
    try:
        if os.path.exists(HEALTH_HISTORY_FILE):
            with open(HEALTH_HISTORY_FILE, 'r', encoding='utf-8') as f:
                history_records = json.load(f)
            print(f"✅ Đã tải lịch sử sức khỏe: {len(history_records)} bản ghi")
        else:
            history_records = []
            print("✅ Khởi tạo lịch sử sức khỏe mới")
    except Exception as e:
        print(f"❌ Lỗi tải lịch sử: {e}")
        history_records = []

def save_health_history():
    try:
        with open(HEALTH_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history_records, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ Lỗi lưu lịch sử: {e}")

def add_health_record():
    """Thêm bản ghi vào lịch sử"""
    global history_records
    try:
        record = {
            'timestamp': datetime.now().isoformat(),
            'bpm': LATEST_BPM,
            'spo2': LATEST_SPO2,
            'posture_status': posture_status,
            'sitting_time': TOTAL_SITTING_TIME_SEC / 60,
            'moving_time': TOTAL_MOVING_TIME_SEC / 60,
            'alert_active': alert_sent_for_current_session,
            'long_incorrect_alert': long_incorrect_alert_sent,
            'long_sitting_alert': long_sitting_alert_sent
        }
        history_records.append(record)
        
        if len(history_records) > MAX_HISTORY_RECORDS:
            history_records = history_records[-MAX_HISTORY_RECORDS:]
        
        if len(history_records) % 10 == 0:
            save_health_history()
            
    except Exception as e:
        print(f"Lỗi thêm bản ghi lịch sử: {e}")

# --- HÀM XỬ LÝ AI CỐT LÕI (34 Features) ---
def extract_features_34(keypoints_obj, img_shape):
    try:
        height, width = img_shape[:2]
        kps = keypoints_obj.data[0].cpu().numpy() 
        normalized_kps = []
        for kp in kps:
            x, y, conf = kp
            if conf > CONFIDENCE_THRESHOLD: 
                normalized_kps.extend([x/width, y/height])
            else: 
                normalized_kps.extend([0, 0])
        while len(normalized_kps) < 34: 
            normalized_kps.append(0)
        return np.array(normalized_kps[:34])
    except: 
        return np.zeros(34)

# --- HÀM DỰ ĐOÁN THEO VÙNG MA THUẬT - ĐÃ TỐI ƯU HÓA ---
def predict_posture_cascaded(frame):
    """Logic Vùng Ma Thuật - Tối ưu hóa tốc độ xử lý"""
    current_status = 2  # Mặc định: Vắng mặt
    
    try:
        h, w = frame.shape[:2]
        
        # 1. ĐỊNH NGHĨA VÙNG GIÁM SÁT (Magic Zone)
        ZONE_X1, ZONE_Y1 = int(w * ZONE_X1_RATIO), int(h * ZONE_Y1_RATIO)
        ZONE_X2, ZONE_Y2 = int(w * ZONE_X2_RATIO), int(h * ZONE_Y2_RATIO)

        # 2. CHẠY YOLO với cấu hình tối ưu hóa tốc độ
        results = yolo_model(frame, verbose=False, conf=0.5, iou=0.5)
        
        best_person_idx = -1
        max_area = 0
        
        if len(results) > 0 and results[0].boxes is not None and len(results[0].boxes) > 0:
            boxes = results[0].boxes.xyxy.cpu().numpy()
            keypoints = results[0].keypoints
            
            # 3. LỌC NGƯỜI TRONG VÙNG MA THUẬT
            for i, box in enumerate(boxes):
                # Tính tâm của người đó
                bx1, by1, bx2, by2 = box[:4]
                center_x = (bx1 + bx2) / 2
                center_y = (by1 + by2) / 2
                
                # Kiểm tra tâm có nằm trong ZONE không?
                if (ZONE_X1 < center_x < ZONE_X2) and (ZONE_Y1 < center_y < ZONE_Y2):
                    # Người này nằm trong vùng giám sát!
                    area = (bx2 - bx1) * (by2 - by1)
                    
                    # Chỉ xét người đủ lớn (tránh nhiễu)
                    if area > MIN_PERSON_AREA and area > max_area:
                        max_area = area
                        best_person_idx = i
        
        # 4. XỬ LÝ NGƯỜI ĐƯỢC CHỌN TRONG VÙNG
        if best_person_idx != -1:
            # CÓ NGƯỜI TRONG VÙNG
            features = extract_features_34(keypoints[best_person_idx], frame.shape).reshape(1, -1)
            
            # AI Tầng 1 (Ngồi hay Đứng?)
            presence = p_clf.predict(p_scaler.transform(features))[0]
            
            if presence == 1:  # Đang ngồi
                # AI Tầng 2 (Tư thế)
                scaled_feat = h_scaler.transform(features)
                current_status = int(h_clf.predict(scaled_feat)[0])
            else:
                current_status = 2  # Đứng/Đi lại trong vùng
                
    except Exception as e:
        print(f"Lỗi AI Vùng Ma Thuật: {e}")
        current_status = 2
        
    return current_status

# --- HÀM XỬ LÝ FRAME CUỐI CÙNG ---
def process_last_frame():
    """Xử lý frame cuối cùng được nhận từ ESP32"""
    global last_frame_received, is_processing, posture_status
    global TOTAL_SITTING_TIME_SEC, TOTAL_MOVING_TIME_SEC
    global incorrect_posture_start_time, alert_sent_for_current_session
    global long_incorrect_alert_sent, long_sitting_alert_sent, sitting_start_time
    global performance_stats, consecutive_absence_count, last_confirmed_status
    
    if last_frame_received is None or is_processing:
        return
    
    is_processing = True
    start_time = time.time()
    
    try:
        # Xử lý AI
        status = predict_posture_cascaded(last_frame_received)
        processing_time = time.time() - start_time
        
        # Cập nhật thống kê hiệu suất
        performance_stats['total_frames_processed'] += 1
        performance_stats['total_processing_time'] += processing_time
        performance_stats['last_frame_time'] = processing_time
        performance_stats['fps'] = 1.0 / processing_time if processing_time > 0 else 0
        
        with data_lock:
            current_time = time.time()
            
            # XỬ LÝ NHIỄU VẮNG MẶT - CHỈ CẬP NHẬT KHI XÁC NHẬN
            if status == 2:  # Vắng mặt
                consecutive_absence_count += 1
                if consecutive_absence_count >= ABSENCE_NOISE_THRESHOLD:
                    # Xác nhận thật sự vắng mặt
                    posture_status = 2
                    last_confirmed_status = 2
                    consecutive_absence_count = ABSENCE_NOISE_THRESHOLD  # Không vượt quá ngưỡng
            else:  # Có người
                consecutive_absence_count = 0
                posture_status = status
                last_confirmed_status = status
            
            # Sử dụng trạng thái đã xác nhận cho logic thời gian
            confirmed_status = last_confirmed_status
            
            # Lấy cấu hình hiện tại
            current_incorrect_threshold = INCORRECT_POSTURE_THRESHOLD_SECONDS
            current_long_incorrect_threshold = LONG_INCORRECT_POSTURE_THRESHOLD_SECONDS
            current_long_sitting_threshold = LONG_SITTING_THRESHOLD_SECONDS
            
            # Logic tính thời gian và cảnh báo - CHỈ DÙNG TRẠNG THÁI ĐÃ XÁC NHẬN
            if confirmed_status == 0 or confirmed_status == 1: 
                TOTAL_SITTING_TIME_SEC += TIME_PER_FRAME_SEC
                if sitting_start_time is None:
                    sitting_start_time = current_time
            else: 
                TOTAL_MOVING_TIME_SEC += TIME_PER_FRAME_SEC
                sitting_start_time = None
                if long_sitting_alert_sent:
                    long_sitting_alert_sent = False

            # LOGIC CẢNH BÁO 3 MỨC ĐỘ - CHỈ DÙNG TRẠNG THÁI ĐÃ XÁC NHẬN
            if confirmed_status == 1:  # Sai tư thế
                if incorrect_posture_start_time is None: 
                    incorrect_posture_start_time = current_time
                
                incorrect_duration = current_time - incorrect_posture_start_time
                
                # CẢNH BÁO MỨC 1: Chỉ bật cảnh báo khi đạt ngưỡng
                if (incorrect_duration >= current_incorrect_threshold and 
                    incorrect_duration < current_long_incorrect_threshold and 
                    not alert_sent_for_current_session):
                    print(f"[{time.strftime('%H:%M:%S')}] 🔔 CẢNH BÁO: NGỒI SAI TƯ THẾ {int(incorrect_duration)} GIÂY!")
                    alert_sent_for_current_session = True
                    posture_status = 1
                
                # CẢNH BÁO MỨC 2: Sai lâu
                elif (incorrect_duration >= current_long_incorrect_threshold and 
                      not long_incorrect_alert_sent):
                    print(f"[{time.strftime('%H:%M:%S')}] 🚨 CẢNH BÁO NGUY HIỂM: NGỒI SAI TƯ THẾ QUÁ LÂU!")
                    long_incorrect_alert_sent = True
                    posture_status = 3
                    alert_sent_for_current_session = True
            
            elif confirmed_status == 0:  # Đúng tư thế
                incorrect_posture_start_time = None
                if alert_sent_for_current_session or long_incorrect_alert_sent:
                    alert_sent_for_current_session = False
                    long_incorrect_alert_sent = False
                    posture_status = 0
            
            else:  # Vắng mặt đã xác nhận
                incorrect_posture_start_time = None
                sitting_start_time = None
                if alert_sent_for_current_session or long_incorrect_alert_sent or long_sitting_alert_sent:
                    alert_sent_for_current_session = False
                    long_incorrect_alert_sent = False
                    long_sitting_alert_sent = False
                    posture_status = 2

            # CẢNH BÁO NGỒI LÂU
            if sitting_start_time is not None and not long_sitting_alert_sent:
                sitting_duration = current_time - sitting_start_time
                if sitting_duration >= current_long_sitting_threshold:
                    print(f"[{time.strftime('%H:%M:%S')}] 🕒 CẢNH BÁO: NGỒI LIÊN TỤC QUÁ LÂU!")
                    long_sitting_alert_sent = True
                    posture_status = 4
        
        # Lưu ảnh mẫu
        if confirmed_status in [0, 1, 3, 4]:
            save_sample_image(last_frame_received, posture_status)
        
        # Lưu vào lịch sử
        add_health_record()
        
        print(f"📸 Frame processed in {processing_time*1000:.1f}ms | Status: {posture_status} | Confirmed: {confirmed_status} | Absence count: {consecutive_absence_count}")
            
    except Exception as e:
        print(f"Lỗi xử lý frame: {e}")
    
    finally:
        is_processing = False
        last_processing_time = time.time()

# ==================== API ROUTES ====================

# Route chính - phục vụ web dashboard
@app.route('/')
def serve_dashboard():
    return send_from_directory('static', 'index.html')

@app.route('/dashboard')
def dashboard():
    return send_from_directory('static', 'index.html')

# Route cho các file static
@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)

# API trạng thái đơn giản cho test kết nối
@app.route('/api/status', methods=['GET'])
def api_status():
    """API đơn giản để test kết nối"""
    return jsonify({
        "status": "online",
        "message": "Server is running",
        "timestamp": time.time()
    }), 200

# API trả về trạng thái đơn giản
@app.route('/api/simple_status', methods=['GET'])
def get_simple_status():
    """API đơn giản cho web dashboard"""
    with data_lock:
        return jsonify({
            "posture_status": posture_status,
            "tracking_active": posture_status in [0, 1, 3, 4],
            "alert_active": alert_sent_for_current_session,
            "long_incorrect_alert": long_incorrect_alert_sent,
            "long_sitting_alert": long_sitting_alert_sent
        }), 200

# API lấy trạng thái tổng hợp cho web
@app.route('/api/get_status', methods=['GET'])
@handle_exceptions  
def get_combined_status():
    """Trả về trạng thái tổng hợp cho web dashboard"""
    try:
        with data_lock:
            sitting_time_min = TOTAL_SITTING_TIME_SEC / 60
            moving_time_min = TOTAL_MOVING_TIME_SEC / 60
            current_posture = posture_status
            current_alert = alert_sent_for_current_session
            
            bad_posture_time = 0
            if incorrect_posture_start_time is not None:
                bad_posture_time = time.time() - incorrect_posture_start_time
            
            continuous_sitting_time = 0
            if sitting_start_time is not None:
                continuous_sitting_time = time.time() - sitting_start_time
        
        with sensor_data_lock:
            current_bpm = LATEST_BPM
            current_spo2 = LATEST_SPO2
            sensor_data_stale = (time.time() - LAST_SENSOR_UPDATE_TIME) > 300 
        
        current_config = get_current_config()
        current_zone_config = get_current_zone_config()
        
        # Tính hiệu suất
        avg_processing_time = performance_stats['total_processing_time'] / performance_stats['total_frames_processed'] if performance_stats['total_frames_processed'] > 0 else 0
        
        return jsonify({
            "bpm": current_bpm if not sensor_data_stale else 0,
            "spo2": current_spo2 if not sensor_data_stale else 0,
            "posture_status": current_posture,
            "sitting_time_min": round(sitting_time_min, 1),
            "moving_time_min": round(moving_time_min, 1),
            "is_alerting": current_alert,
            "tracking_active": current_posture in [0, 1, 3, 4],
            "alert_active": current_alert,
            "long_incorrect_alert": long_incorrect_alert_sent,
            "long_sitting_alert": long_sitting_alert_sent,
            "bad_posture_time_sec": round(bad_posture_time, 1),
            "continuous_sitting_time_sec": round(continuous_sitting_time, 1),
            "config": current_config,
            "zone_config": current_zone_config,
            "performance": {
                "fps": round(performance_stats['fps'], 1),
                "avg_processing_time_ms": round(avg_processing_time * 1000, 1),
                "total_frames": performance_stats['total_frames_processed']
            },
            "timestamp": time.time(),
            "absence_count": consecutive_absence_count
        }), 200
    except Exception as e:
        print(f"Lỗi get_status: {e}")
        return jsonify({"error": str(e), "status": "error"}), 500

# Route nhận ảnh từ ESP32 - XỬ LÝ LAST FRAME
@app.route('/detect', methods=['POST'])
@handle_exceptions
def receive_image():
    """Route nhận ảnh từ ESP32 - Xử lý last frame"""
    global last_frame_received
    
    try:
        start_time = time.time()
        data = request.data
        if not data: 
            return jsonify({"status": "error"}), 400
        
        # Decode ảnh
        frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if frame is None: 
            return jsonify({"status": "error"}), 400

        frame = cv2.flip(frame, 1)

        if frame.shape[:2] != (INPUT_HEIGHT, INPUT_WIDTH):
            frame = cv2.resize(frame, TARGET_SIZE)
        
        # CẬP NHẬT LAST FRAME - KHÔNG DÙNG QUEUE
        last_frame_received = frame
        
        # XỬ LÝ FRAME NGAY LẬP TỨC
        process_last_frame()

        # Trả về response nhanh chóng
        with data_lock:
            current_config = get_current_config()
            
            current_bad_posture_time = 0
            if incorrect_posture_start_time is not None:
                current_bad_posture_time = time.time() - incorrect_posture_start_time
            
            # QUY TẮC CẢNH BÁO
            if long_sitting_alert_sent:
                resp_status = 4
                message = "LONG_SITTING_ALERT"
            elif long_incorrect_alert_sent:
                resp_status = 3
                message = "LONG_BAD_POSTURE_ALERT"
            elif alert_sent_for_current_session and current_bad_posture_time >= current_config['incorrect_posture_threshold']:
                resp_status = 1
                message = "BAD_POSTURE_ALERT"
            elif posture_status == 0:
                resp_status = 0
                message = "GOOD_POSTURE"
            elif posture_status == 1 and current_bad_posture_time < current_config['incorrect_posture_threshold']:
                resp_status = 0
                message = "GOOD_POSTURE"
            else:
                resp_status = posture_status
                message = "ABSENT" if posture_status == 2 else "GOOD_POSTURE"
                
            sitting_time = TOTAL_SITTING_TIME_SEC / 60
            moving_time = TOTAL_MOVING_TIME_SEC / 60
            
            bad_posture_time = current_bad_posture_time
            
            continuous_sitting_time = 0
            if sitting_start_time is not None:
                continuous_sitting_time = time.time() - sitting_start_time
            
        processing_time = time.time() - start_time
        
        return jsonify({
            "status_code": resp_status,
            "message": message,
            "sitting_time_min": sitting_time,
            "moving_time_min": moving_time,
            "bad_posture_time_sec": round(bad_posture_time, 1),
            "continuous_sitting_time_sec": round(continuous_sitting_time, 1),
            "alert_active": alert_sent_for_current_session,
            "long_incorrect_alert": long_incorrect_alert_sent,
            "long_sitting_alert": long_sitting_alert_sent,
            "current_config": current_config,
            "ai_posture_status": posture_status,
            "threshold": current_config['incorrect_posture_threshold'],
            "processing_time_ms": round(processing_time * 1000, 1),
            "absence_count": consecutive_absence_count
        }), 200
        
    except Exception as e:
        print(f"Lỗi nhận ảnh: {e}")
        return jsonify({"status": "error"}), 500

# Thêm API cho cấu hình vùng
@app.route('/api/set_zone_config', methods=['POST'])
@handle_exceptions
def set_zone_config():
    """API thiết lập cấu hình vùng ma thuật"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON data received"}), 400
        
        zone_x1_ratio = data.get('zone_x1_ratio')
        zone_y1_ratio = data.get('zone_y1_ratio')
        zone_x2_ratio = data.get('zone_x2_ratio')
        zone_y2_ratio = data.get('zone_y2_ratio')
        
        # Validate dữ liệu
        if zone_x1_ratio is not None and (not isinstance(zone_x1_ratio, (int, float)) or zone_x1_ratio < 0 or zone_x1_ratio > 1):
            return jsonify({"success": False, "error": "zone_x1_ratio phải là số từ 0 đến 1"}), 400
        
        if zone_y1_ratio is not None and (not isinstance(zone_y1_ratio, (int, float)) or zone_y1_ratio < 0 or zone_y1_ratio > 1):
            return jsonify({"success": False, "error": "zone_y1_ratio phải là số từ 0 đến 1"}), 400
            
        if zone_x2_ratio is not None and (not isinstance(zone_x2_ratio, (int, float)) or zone_x2_ratio < 0 or zone_x2_ratio > 1):
            return jsonify({"success": False, "error": "zone_x2_ratio phải là số từ 0 đến 1"}), 400
            
        if zone_y2_ratio is not None and (not isinstance(zone_y2_ratio, (int, float)) or zone_y2_ratio < 0 or zone_y2_ratio > 1):
            return jsonify({"success": False, "error": "zone_y2_ratio phải là số từ 0 đến 1"}), 400
        
        # Kiểm tra tính hợp lệ của vùng
        if (zone_x1_ratio is not None and zone_x2_ratio is not None and zone_x1_ratio >= zone_x2_ratio):
            return jsonify({"success": False, "error": "zone_x2_ratio phải lớn hơn zone_x1_ratio"}), 400
            
        if (zone_y1_ratio is not None and zone_y2_ratio is not None and zone_y1_ratio >= zone_y2_ratio):
            return jsonify({"success": False, "error": "zone_y2_ratio phải lớn hơn zone_y1_ratio"}), 400
        
        # Cập nhật cấu hình
        zone_config_updates = {}
        with data_lock:
            if zone_x1_ratio is not None:
                zone_config_updates["zone_x1_ratio"] = float(zone_x1_ratio)
                
            if zone_y1_ratio is not None:
                zone_config_updates["zone_y1_ratio"] = float(zone_y1_ratio)
                
            if zone_x2_ratio is not None:
                zone_config_updates["zone_x2_ratio"] = float(zone_x2_ratio)
                
            if zone_y2_ratio is not None:
                zone_config_updates["zone_y2_ratio"] = float(zone_y2_ratio)
        
        if zone_config_updates:
            current_zone_config = get_current_zone_config()
            current_zone_config.update(zone_config_updates)
            if save_zone_config(current_zone_config):
                print(f"✅ Đã cập nhật cấu hình vùng từ web: {zone_config_updates}")
                return jsonify({
                    "success": True, 
                    "message": "Cấu hình vùng đã được cập nhật",
                    "new_zone_config": current_zone_config
                }), 200
            else:
                return jsonify({"success": False, "error": "Lỗi khi lưu cấu hình vùng"}), 500
        else:
            return jsonify({"success": False, "error": "Không có thông tin cấu hình vùng nào được cung cấp"}), 400
            
    except Exception as e:
        print(f"Lỗi set_zone_config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/get_zone_config', methods=['GET'])
@handle_exceptions
def get_zone_config():
    """API lấy cấu hình vùng hiện tại"""
    try:
        zone_config = get_current_zone_config()
        return jsonify({
            "success": True,
            "zone_config": zone_config
        }), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# API CẤU HÌNH
@app.route('/api/set_config', methods=['POST'])
@handle_exceptions
def set_config():
    """API thiết lập cấu hình hệ thống"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON data received"}), 400
        
        incorrect_threshold = data.get('incorrect_posture_threshold')
        long_incorrect_threshold = data.get('long_incorrect_threshold')
        long_sitting_threshold = data.get('long_sitting_threshold')
        
        # Validate dữ liệu
        if incorrect_threshold is not None and (not isinstance(incorrect_threshold, int) or incorrect_threshold < 2):
            return jsonify({"success": False, "error": "incorrect_posture_threshold phải là số nguyên >= 5"}), 400
        
        if long_incorrect_threshold is not None and (not isinstance(long_incorrect_threshold, int) or long_incorrect_threshold < 10):
            return jsonify({"success": False, "error": "long_incorrect_threshold phải là số nguyên >= 30"}), 400
            
        if long_sitting_threshold is not None and (not isinstance(long_sitting_threshold, int) or long_sitting_threshold < 300):
            return jsonify({"success": False, "error": "long_sitting_threshold phải là số nguyên >= 300"}), 400
        
        # Cập nhật cấu hình
        config_updates = {}
        with data_lock:
            if incorrect_threshold is not None:
                config_updates["incorrect_posture_threshold"] = incorrect_threshold
                
            if long_incorrect_threshold is not None:
                config_updates["long_incorrect_threshold"] = long_incorrect_threshold
                
            if long_sitting_threshold is not None:
                config_updates["long_sitting_threshold"] = long_sitting_threshold
        
        if config_updates:
            current_config = get_current_config()
            current_config.update(config_updates)
            if save_config(current_config):
                print(f"✅ Đã cập nhật cấu hình từ web: {config_updates}")
                return jsonify({
                    "success": True, 
                    "message": "Cấu hình đã được cập nhật",
                    "new_config": current_config
                }), 200
            else:
                return jsonify({"success": False, "error": "Lỗi khi lưu cấu hình"}), 500
        else:
            return jsonify({"success": False, "error": "Không có thông tin cấu hình nào được cung cấp"}), 400
            
    except Exception as e:
        print(f"Lỗi set_config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/get_config', methods=['GET'])
@handle_exceptions
def get_config():
    """API lấy cấu hình hiện tại"""
    try:
        config = get_current_config()
        return jsonify({
            "success": True,
            "config": config
        }), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# API thống kê hiệu suất
@app.route('/api/performance', methods=['GET'])
@handle_exceptions
def get_performance():
    """API lấy thông tin hiệu suất hệ thống"""
    try:
        avg_processing_time = performance_stats['total_processing_time'] / performance_stats['total_frames_processed'] if performance_stats['total_frames_processed'] > 0 else 0
        
        return jsonify({
            "success": True,
            "performance": {
                "total_frames_processed": performance_stats['total_frames_processed'],
                "average_processing_time_ms": round(avg_processing_time * 1000, 1),
                "last_frame_time_ms": round(performance_stats['last_frame_time'] * 1000, 1),
                "current_fps": round(performance_stats['fps'], 1),
                "system_uptime_seconds": round(time.time() - start_time, 1)
            }
        }), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# Route nhận sensor data từ ESP32
@app.route('/sensor_data', methods=['POST'])
@handle_exceptions
def receive_sensor():
    """Route nhận sensor data từ ESP32"""
    global LATEST_BPM, LATEST_SPO2, LAST_SENSOR_UPDATE_TIME
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No JSON data received"}), 400
            
        bpm = data.get('bpm')
        spo2 = data.get('spo2')
        if bpm is None or spo2 is None:
            return jsonify({"status": "error", "message": "Missing 'bpm' or 'spo2'"}), 400
            
        with sensor_data_lock:
            LATEST_BPM = bpm
            LATEST_SPO2 = spo2
            LAST_SENSOR_UPDATE_TIME = time.time()
            
        print(f"✅ Đã nhận dữ liệu sensor: BPM={bpm}, SpO2={spo2}")
        return jsonify({"status": "ok", "message": "Sensor data received"}), 200
        
    except Exception as e:
        print(f"Lỗi nhận sensor: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# Các API khác giữ nguyên...
@app.route('/api/get_history', methods=['GET'])
@handle_exceptions
def get_history():
    """Trả về lịch sử dữ liệu"""
    try:
        cutoff_time = datetime.now() - timedelta(days=7)
        filtered_history = [
            record for record in history_records 
            if datetime.fromisoformat(record['timestamp']) > cutoff_time
        ]
        
        formatted_history = []
        for record in filtered_history[-20:]: 
            formatted_history.append({
                "timestamp": record["timestamp"],
                "bpm": record["bpm"],
                "spo2": record["spo2"],
                "posture_status": record["posture_status"],
                "sitting_time": record["sitting_time"],
                "moving_time": record["moving_time"],
                "long_incorrect_alert": record.get("long_incorrect_alert", False),
                "long_sitting_alert": record.get("long_sitting_alert", False)
            })
        
        return jsonify(formatted_history), 200
    except Exception as e:
        print(f"Lỗi get_history: {e}")
        return jsonify([]), 500

@app.route('/api/export_history', methods=['GET'])
@handle_exceptions
def export_history():
    """Export lịch sử dạng Event Log (Chỉ ghi khi trạng thái thay đổi, bỏ qua bộ đếm thời gian)"""
    try:
        # 1. Header file CSV (có BOM \ufeff để sửa lỗi font tiếng Việt)
        csv_data = "\ufeffTimestamp,BPM,SpO2,Posture Status,Sitting Time (min),Moving Time (min),Long Incorrect Alert,Long Sitting Alert\n"
        
        last_recorded_signature = None
        
        # Lấy toàn bộ lịch sử để có cái nhìn tổng quan
        for record in history_records: 
            # --- Xử lý hiển thị (Format dữ liệu) ---
            try:
                dt_obj = datetime.fromisoformat(record["timestamp"])
                time_str = dt_obj.strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:
                time_str = record["timestamp"]

            if record["posture_status"] == 0:
                posture_text = "Đúng (Correct)"
            elif record["posture_status"] == 1:
                posture_text = "Sai (Incorrect)"
            elif record["posture_status"] == 3:
                posture_text = "Sai Lâu (Long Incorrect)"
            elif record["posture_status"] == 4:
                posture_text = "Ngồi Lâu (Long Sitting)"
            else:
                posture_text = "Vắng (Absent)"

            long_incorrect = "CÓ" if record.get("long_incorrect_alert", False) else "KHÔNG"
            long_sitting = "CÓ" if record.get("long_sitting_alert", False) else "KHÔNG"
            
            sit_time_str = f"{record['sitting_time']:.1f}"
            move_time_str = f"{record['moving_time']:.1f}"

            # --- [QUAN TRỌNG] LOGIC SO SÁNH MỚI ---
            # Chỉ tạo dòng mới khi thay đổi: BPM, SpO2, Tư thế, hoặc Cảnh báo.
            # ĐÃ BỎ: sit_time_str và move_time_str khỏi tuple so sánh.
            current_signature = (
                record['bpm'],
                record['spo2'],
                record['posture_status'],
                long_incorrect,
                long_sitting
            )

            # Chỉ ghi vào CSV nếu trạng thái khác với dòng trước đó
            if current_signature != last_recorded_signature:
                csv_data += f"{time_str},{record['bpm']},{record['spo2']},{posture_text},{sit_time_str},{move_time_str},{long_incorrect},{long_sitting}\n"
                last_recorded_signature = current_signature
        
        # Tạo file response
        filename = f"health_event_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

        return Response(
            csv_data,
            mimetype="text/csv",
            headers={
                "Content-disposition": f"attachment; filename={filename}",
                "Content-Type": "text/csv; charset=utf-8-sig"
            }
        )
    except Exception as e:
        print(f"Lỗi export_history: {e}")
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

@app.route('/api/reset_alerts', methods=['POST'])
@handle_exceptions
def reset_alerts():
    """API reset tất cả cảnh báo"""
    global alert_sent_for_current_session, long_incorrect_alert_sent, long_sitting_alert_sent
    global incorrect_posture_start_time, sitting_start_time, consecutive_absence_count
    
    with data_lock:
        alert_sent_for_current_session = False
        long_incorrect_alert_sent = False
        long_sitting_alert_sent = False
        incorrect_posture_start_time = None
        sitting_start_time = None
        consecutive_absence_count = 0
        
    print("🧹 Đã reset tất cả cảnh báo và bộ đếm thời gian")
    return jsonify({"status": "alerts_reset", "message": "Đã reset tất cả cảnh báo"}), 200

# ✅ Thêm endpoint health check đơn giản
@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": time.time(),
        "version": "1.0.0"
    }), 200

# ✅ Giới hạn kích thước request
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

@app.route('/sample_images/<path:path>')
def serve_sample_images(path):
    """Phục vụ ảnh mẫu"""
    return send_from_directory(SAMPLE_IMAGE_DIR, path)

# API cho performance chi tiết
@app.route('/api/detailed_performance', methods=['GET'])
@handle_exceptions
def get_detailed_performance():
    """API lấy thông tin hiệu suất chi tiết"""
    try:
        with data_lock:
            current_time = time.time()
            uptime = current_time - start_time
            
            avg_processing_time = (performance_stats['total_processing_time'] / 
                                 performance_stats['total_frames_processed'] 
                                 if performance_stats['total_frames_processed'] > 0 else 0)
            
            return jsonify({
                'success': True,
                'performance': {
                    'total_frames_processed': performance_stats['total_frames_processed'],
                    'average_processing_time_ms': round(avg_processing_time * 1000, 2),
                    'last_frame_time_ms': round(performance_stats['last_frame_time'] * 1000, 2),
                    'current_fps': round(performance_stats['fps'], 2),
                    'system_uptime_seconds': round(uptime, 1),
                    'system_uptime_human': str(timedelta(seconds=int(uptime))),
                    'processing_load_percent': min(100, round(avg_processing_time * performance_stats['fps'] * 100, 1))
                },
                'system_info': {
                    'gpu_available': torch.cuda.is_available(),
                    'gpu_name': torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU',
                    'python_version': os.sys.version.split()[0],
                    'platform': os.sys.platform
                }
            }), 200
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/get_sample_images', methods=['GET'])
@handle_exceptions
def get_sample_images():
    """API lấy ảnh mẫu mới nhất"""
    try:
        correct_images = []
        incorrect_images = []
        
        # Lấy ảnh mẫu từ thư mục
        for posture_dir, image_list in [(CORRECT_POSTURE_DIR, correct_images), 
                                      (INCORRECT_POSTURE_DIR, incorrect_images)]:
            if os.path.exists(posture_dir):
                image_files = glob.glob(os.path.join(posture_dir, '*.jpg'))
                # Sắp xếp theo thời gian, lấy ảnh mới nhất
                image_files.sort(key=os.path.getmtime, reverse=True)
                
                for img_file in image_files[:5]:  # Lấy 5 ảnh mới nhất
                    filename = os.path.basename(img_file)
                    timestamp = datetime.fromtimestamp(os.path.getmtime(img_file))
                    image_list.append({
                        'filename': filename,
                        'url': f'/sample_images/{os.path.basename(posture_dir)}/{filename}',
                        'timestamp': timestamp.strftime('%H:%M:%S'),
                        'posture_type': 'correct' if posture_dir == CORRECT_POSTURE_DIR else 'incorrect'
                    })
        
        return jsonify({
            'success': True,
            'sample_images': correct_images + incorrect_images
        }), 200
        
    except Exception as e:
        print(f"Lỗi lấy ảnh mẫu: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# --- HIỂN THỊ PREVIEW VỚI VÙNG MA THUẬT (CHỈ CHO OPENCV) ---
def display_preview_loop():
    """Hiển thị preview window với thông tin thời gian thực - CHỈ DÀNH CHO OPENCV"""
    global posture_status, alert_sent_for_current_session
    global LATEST_BPM, LATEST_SPO2, LAST_SENSOR_UPDATE_TIME
    global incorrect_posture_start_time, long_incorrect_alert_sent, long_sitting_alert_sent
    global sitting_start_time, performance_stats, consecutive_absence_count, last_frame_received
    
    cv2.namedWindow("AI Preview - Magic Zone", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("AI Preview - Magic Zone", 640, 480)
    
    while True:
        img = None
        if last_frame_received is not None: 
            img = last_frame_received.copy()
        
        if img is not None:
            h, w = img.shape[:2]
            
            # VẼ VÙNG MA THUẬT
            ZX1, ZY1 = int(w * ZONE_X1_RATIO), int(h * ZONE_Y1_RATIO)
            ZX2, ZY2 = int(w * ZONE_X2_RATIO), int(h * ZONE_Y2_RATIO)
            
            cv2.rectangle(img, (ZX1, ZY1), (ZX2, ZY2), (0, 255, 255), 2)
            cv2.putText(img, "MONITORING ZONE", (ZX1, ZY1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
            
            with data_lock:
                current_status = posture_status
                current_alert = alert_sent_for_current_session
                current_long_incorrect_alert = long_incorrect_alert_sent
                current_long_sitting_alert = long_sitting_alert_sent
                current_config = get_current_config()
                
                bad_posture_time = 0
                if incorrect_posture_start_time is not None:
                    bad_posture_time = time.time() - incorrect_posture_start_time
                
                continuous_sitting_time = 0
                if sitting_start_time is not None:
                    continuous_sitting_time = time.time() - sitting_start_time
            
            # Hiển thị thông tin chi tiết
            if current_status in [0, 1, 3, 4]:
                if current_status == 0:
                    status_text = "CORRECT"
                    color = (0, 255, 0)
                elif current_status == 1:
                    status_text = "INCORRECT"
                    color = (0, 165, 255)
                elif current_status == 3:
                    status_text = "LONG INCORRECT!"
                    color = (0, 0, 255)
                elif current_status == 4:
                    status_text = "LONG SITTING!"
                    color = (255, 0, 255)
                
                cv2.putText(img, f"Status: {status_text}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
                
                # Hiển thị thời gian sai tư thế và ngưỡng
                if current_status in [1, 3]:
                    cv2.putText(img, f"Bad posture: {bad_posture_time:.1f}s / {current_config['incorrect_posture_threshold']}s", 
                               (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
                
                # Hiển thị thời gian ngồi liên tục
                cv2.putText(img, f"Sitting: {continuous_sitting_time:.1f}s", (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
                
                # Hiển thị trạng thái gửi đến ESP32
                if current_long_sitting_alert:
                    esp_status = "LONG_SITTING_ALERT"
                elif current_long_incorrect_alert:
                    esp_status = "LONG_BAD_POSTURE_ALERT"
                elif current_alert and bad_posture_time >= current_config['incorrect_posture_threshold']:
                    esp_status = "BAD_POSTURE_ALERT"
                elif current_status == 1 and bad_posture_time < current_config['incorrect_posture_threshold']:
                    esp_status = "GOOD_POSTURE (Counting...)"
                else:
                    esp_status = "GOOD_POSTURE"
                
                cv2.putText(img, f"ESP32: {esp_status}", (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                    
                cv2.putText(img, "Sit in the YELLOW ZONE", (10, 450), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            else:
                cv2.putText(img, "Please sit in the YELLOW ZONE", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 0), 2)
            
            # Hiển thị thông tin nhiễu vắng mặt
            cv2.putText(img, f"Absence count: {consecutive_absence_count}/{ABSENCE_NOISE_THRESHOLD}", 
                       (10, 150), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
            
            with sensor_data_lock:
                current_time = time.time()
                if current_time - LAST_SENSOR_UPDATE_TIME < 30: 
                    cv2.putText(img, f"HR: {LATEST_BPM} | SpO2: {LATEST_SPO2}%", (10, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            
            # Hiển thị thông tin hiệu suất
            avg_processing_time = performance_stats['total_processing_time'] / performance_stats['total_frames_processed'] if performance_stats['total_frames_processed'] > 0 else 0
            cv2.putText(img, f"FPS: {performance_stats['fps']:.1f} | Avg: {avg_processing_time*1000:.1f}ms", 
                       (10, 200), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
            
            # Hiển thị cấu hình hiện tại
            cv2.putText(img, f"Config: Incorrect={current_config['incorrect_posture_threshold']}s, Long={current_config['long_incorrect_threshold']}s, Sit={current_config['long_sitting_threshold']}s", 
                       (10, 420), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)
            
            cv2.imshow("AI Preview - Magic Zone", img)
        
        key = cv2.waitKey(10) & 0xFF
        if key == ord('q'):
            break
        elif key == ord('c'):
            # Reset tất cả cảnh báo
            with data_lock:
                alert_sent_for_current_session = False
                long_incorrect_alert_sent = False
                long_sitting_alert_sent = False
                incorrect_posture_start_time = None
                sitting_start_time = None
                consecutive_absence_count = 0
            print("🧹 Đã clear tất cả cảnh báo và bộ đếm nhiễu")
        elif key == ord('r'):
            # Reset thống kê hiệu suất
            with data_lock:
                performance_stats['total_frames_processed'] = 0
                performance_stats['total_processing_time'] = 0
                performance_stats['last_frame_time'] = 0
                performance_stats['fps'] = 0
            print("📊 Đã reset thống kê hiệu suất")
            
    cv2.destroyAllWindows()
    os._exit(0)

# --- KHỞI ĐỘNG SERVER ---
if __name__ == '__main__':
    start_time = time.time()
    load_config()
    load_health_history()
    
    # KHÔNG CÒN LUỒNG XỬ LÝ NỀN - XỬ LÝ TRỰC TIẾP TRONG API
    
    if SHOW_PREVIEW_WINDOW:
        threading.Thread(target=display_preview_loop, daemon=True).start()
    
    from waitress import serve
    
    current_config = get_current_config()
    current_zone_config = get_current_zone_config()
    
    print(f"\n🚀 SERVER XỬ LÝ LAST FRAME ĐANG CHẠY TẠI: http://0.0.0.0:8000")
    print(f"📊 Web Dashboard: http://0.0.0.0:8000/")
    print(f"📸 API nhận ảnh từ ESP32: POST http://0.0.0.0:8000/detect")
    print(f"💓 API nhận sensor: POST http://0.0.0.0:8000/sensor_data")
    print(f"⚙️  API Cấu hình: GET/POST http://0.0.0.0:8000/api/get_config | /api/set_config")
    print(f"🎯 API Vùng giám sát: GET/POST http://0.0.0.0:8000/api/get_zone_config | /api/set_zone_config")
    print(f"📈 API Hiệu suất: GET http://0.0.0.0:8000/api/performance")
    print(f"📊 API Lịch sử: GET http://0.0.0.0:8000/api/get_history")
    print(f"📥 API Export: GET http://0.0.0.0:8000/api/export_history")
    print(f"🧹 API Reset: POST http://0.0.0.0:8000/api/reset_alerts")
    print(f"❤️  API Health Check: GET http://0.0.0.0:8000/api/health")
    print(f"🎪 PHƯƠNG PHÁP: VÙNG MA THUẬT - Chỉ giám sát người ngồi trong vùng vàng")
    print(f"📏 Vùng giám sát hiện tại: X={current_zone_config['zone_x1_ratio']*100}%-{current_zone_config['zone_x2_ratio']*100}%, Y={current_zone_config['zone_y1_ratio']*100}%-{current_zone_config['zone_y2_ratio']*100}%")
    print(f"🔄 XỬ LÝ: LAST FRAME - Không dùng hàng đợi")
    print(f"🔇 CHỐNG NHIỄU: Cần {ABSENCE_NOISE_THRESHOLD} frame vắng mặt liên tiếp để xác nhận")
    print(f"🚫 PREVIEW WEB: Đã tắt hoàn toàn - Chỉ hiển thị OpenCV")
    print(f"🛡️  BẢO VỆ: Tất cả API đều có exception handling")
    
    # Cấu hình Waitress cho production
    serve(
        app, 
        host='0.0.0.0', 
        port=8000,
        threads=8,
        connection_limit=1000,
        asyncore_use_poll=True
    )
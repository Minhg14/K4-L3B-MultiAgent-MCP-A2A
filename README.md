# K4 L3B — Multi-Agent MCP + A2A

## Mục tiêu

Xây dựng hệ thống multi-agent điều tra khiếu nại thương mại điện tử.

Ngoài kết luận nghiệp vụ, yêu cầu cần phải xử lý xử lý entity resolution, customer context, shipment/payment analysis, source conflict và hiệu quả sử dụng MCP.

## Dữ liệu

Tham khảo dữ liệu tại: https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce

## Quy tắc đặt tên

Làm nhóm hoặc cá nhân, khi fork về các bạn giữ nguyên tên gốc repo, không đổi tên

## 1. Cài đặt

Yêu cầu Python 3.11 trở lên.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Kiểm tra:

```bash
pytest -q
day09 --help
```

## 2. Đăng ký team

1. Mở `/register` trên Competition Workspace.
2. Điền tên team, mã học viên và các thành viên.
3. Nhập registration code của lớp.
4. Lưu Team API Key dạng `sk-team-...` được hiển thị sau khi đăng ký.

Điền thông tin thật vào `.env`:

```dotenv
COMPETITION_API_URL=http://127.0.0.1:8081
COMPETITION_TEAM_API_KEY=sk-team-your_key
MCP_ENDPOINT=http://127.0.0.1:8001/mcp
```

## 3. Tải input

Tải ZIP input **L3B** từ GitHub Release và giải nén vào root repo:

```bash
unzip l3b-inputs-<version>.zip -d .
day09 validate-inputs
```

Cấu trúc đúng:

```text
case-set.json
inputs/
├── L3B_CASE_001.json
├── ...
└── L3B_CASE_100.json
```

Loader cũng hỗ trợ bộ input đang lưu theo version trong repo, ví dụ
`inputs/l3b-inputs-v1/inputs/`, khi manifest ở root khớp chính xác với 100 case
trong thư mục đó. Nếu có nhiều thư mục input lồng nhau khớp manifest, loader sẽ
báo lỗi để tránh chọn nhầm release.

Một số case không cung cấp exact order ID. Agent phải dùng candidate và evidence để resolve entity.

## 4. Sử dụng MCP

MCP Gateway cung cấp evidence về order, customer, product, shipment, payment, refund và policy. Mọi call đều được server audit theo team và case.

Xem các tool hiện có:

```bash
day09 mcp-tools
```

Ví dụ gọi tool trong `workflow.py`:

```python
evidence = await gateway.call(
    "get_customer_history",
    case_id=case["case_id"],
    customer_unique_id=customer_unique_id,
)

evidence_ref = evidence["evidence_ref"]
customer_data = evidence["data"]
```

Khi dùng evidence, ghi lại trong trace:

```python
trace.emit(
    case_id=case["case_id"],
    event_type="tool_result_consumed",
    actor="entity-agent",
    tool_name="get_customer_history",
    evidence_refs=[evidence_ref],
)
```

Quy tắc quan trọng:

- luôn truyền đúng `case_id`;
- dùng tool discovery, không đoán tên tool;
- không sửa hoặc tự tạo `evidence_ref`;
- không dùng evidence chéo case;
- giới hạn retry, cache trong phạm vi case và tránh gọi tool thừa.

Tất cả MCP calls đều được audit và có thể ảnh hưởng điểm efficiency, kể cả call không được đưa vào output.

## 5. Xây dựng multi-agent workflow

Triển khai tại:

```text
src/student_agent/workflow.py
```

Hàm chính:

```python
async def solve_case(case, gateway, trace) -> dict:
    ...
```

Có thể tổ chức các vai trò:

- entity/customer agent;
- coordinator;
- order/product agent;
- shipment agent;
- payment/refund agent;
- policy hoặc conflict agent;
- verifier.

Competition không chấm tên framework hay số lượng class. Scorer đánh giá output, evidence, efficiency và sự phối hợp thể hiện trong trace.

Trace chỉ ghi sự kiện quan sát được như `task_assigned`, `handoff`, `tool_result_consumed`, `verification_completed`.

Hoàn thiện mô tả thiết kế trong `ARCHITECTURE.md`.

### NVIDIA model (tùy chọn)

Workflow có thể gửi các facts đã xác minh, đã loại định danh tới NVIDIA endpoint. Cài key
cục bộ bằng wizard (key được nhập ẩn và ghi vào `.env`):

```bash
bash scripts/setup_nvidia.sh
```

Mặc định wizard cấu hình `nvidia/llama-3.1-nemotron-safety-guard-8b-v3` và tối đa 100
lần gọi model trong một lần chạy. Lưu ý model này được thiết kế cho kiểm duyệt an toàn nội dung,
không phải phân loại khiếu nại; workflow chỉ nhận lựa chọn nếu model trả về một issue hợp lệ,
còn lại dùng quyết định xác định sẵn. NVIDIA cho biết endpoint thử nghiệm có thể ghi lại input
và output, vì vậy không gửi dữ liệu khách hàng nhạy cảm. NVIDIA không công bố một mức RPM cố định
trong tài liệu endpoint mà mình kiểm tra; giới hạn thực tế phụ thuộc quyền truy cập API.

## 6. Chạy và kiểm tra

```bash
day09 run
day09 validate
```

Kết quả được tạo tại:

```text
outputs/<case_id>.json
traces/trace.jsonl
```

Nếu output pass schema nhưng điểm thấp, cần kiểm tra semantic, entity resolution, evidence, consistency, confidence, workflow và số MCP calls.

## 7. Đóng gói và nộp bài

```bash
day09 package --output dist/submission.zip
```

ZIP chỉ được chứa:

```text
manifest.json
trace.jsonl
outputs/<case_id>.json
```

Không đưa source, input, `.env`, API key hoặc debug log vào ZIP. Sau đó upload `dist/submission.zip` tại workspace `/l3b` và chọn submission muốn dùng làm final.

## Tiêu chí chấm điểm công khai

| Thành phần                                     | Trọng số |
| ---------------------------------------------- | -------: |
| Độ đúng nghiệp vụ (`semantic`)                 |      40% |
| Chất lượng bằng chứng (`evidence`)             |      15% |
| Evidence đúng MCP audit (`provenance`)         |      15% |
| Tính nhất quán giữa các field (`consistency`)  |      10% |
| Đúng JSON Schema (`schema`)                    |       5% |
| Confidence hợp lý (`calibration`)              |       5% |
| Quy trình multi-agent trong trace (`workflow`) |       5% |
| Hiệu quả gọi tool (`efficiency`)               |       5% |

Case có thể nhận 0 điểm nếu:

- sai `case_id` hoặc output không thể chấm theo schema;
- thiếu evidence bắt buộc;
- evidence ref không tồn tại;
- evidence thuộc team, run hoặc case khác.

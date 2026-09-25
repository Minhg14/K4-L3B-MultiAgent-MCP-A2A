# L3B Architecture Record

## 1. System overview

Luồng điều tra khiếu nại thương mại điện tử Multi-Agent:

```text
Input → Entity Resolver → Coordinator → Specialists (Shipment, Payment, Order) → Policy/Conflict → Verifier → Output
             │                              │                                             │             │
             └──────────────────────────── MCP Gateway ───────────────────────────────────┴─────────── Trace
```

1. **Coordinator** nhận case, điều phối phân rã tác vụ và chuyển giao kết quả giữa các tác nhân.
2. **Entity Resolver** xác định danh tính khách hàng và đơn hàng hợp lệ thông qua candidate orders và customer hint.
3. **Specialists (Shipment, Financial, Order)** song song thu thập bằng chứng từ MCP Gateway.
4. **Policy & Conflict Specialist** đối soát chính sách bồi thường và xử lý xung đột giữa các nguồn dữ liệu.
5. **Verifier Agent** kiểm tra tính toàn vẹn (invariants), schema hợp lệ và chuẩn hóa confidence trước khi tạo output.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| `coordinator` | `case` object | Quản lý vòng đời vụ việc, phân công nhiệm vụ và tổng hợp kết luận | Không trực tiếp gọi data tool | Giao việc cho các specialist, nhận kết quả |
| `entity_resolver` | `candidate_order_ids`, `customer_unique_id_hint` | Xác minh candidate hợp lệ, loại trừ candidate giả, truy xuất customer history | `get_order`, `get_customer_history` | `resolved_order_ids`, `rejected_candidates`, `customer_context` |
| `order_specialist` | `target_order_id`, `investigation_scope` | Trích xuất items, sản phẩm và danh mục liên quan | `get_order_items`, `get_product_context` | `item_ids`, `seller_ids`, ngữ cảnh sản phẩm |
| `shipment_specialist` | `target_order_id` | Phân tích mốc thời gian giao nhận, quy kết trách nhiệm giao trễ | `get_shipment_summary`, `get_sellers` | `shipment_analysis`, `late_seller_ids` |
| `financial_specialist` | `target_order_id` | Đối soát giao dịch, hoàn tiền và tổng giá trị BRL | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `payment_analysis`, số tiền `captured`/`refunded`/`refundable` |
| `policy_agent` | `policy_version`, claim topics | Khớp quy tắc bồi thường, bên chịu trách nhiệm và phát hiện mâu thuẫn | `get_policy` | `primary_issue`, `case_status`, `financial_resolution`, `data_conflicts` |
| `verifier` | Kết quả tổng hợp | Kiểm chứng tính nhất quán giữa các trường, kiểm tra schema | Không gọi MCP tool | Output JSON hợp chuẩn cuối cùng |

## 3. Entity resolution và A2A protocol

- **Phân loại Candidate**: Kiểm tra từng candidate order ID bằng `get_order`. Candidate trả về đơn hàng hợp lệ được đưa vào `resolved_order_ids`; candidate thất bại bị đưa vào `rejected_candidates`.
- **Confidence Calibration**: Đạt `0.95` nếu có đúng 1 order được giải quyết; `0.70` nếu có tranh chấp (ambiguous); `0.10` nếu không tìm thấy.
- **Trace Protocol**: Mỗi bước chuyển giao giữa các Agent đều phát sự kiện `handoff` hoặc `task_assigned` với `case_id` tương ứng, không lưu chain-of-thought vào trace.

## 4. Evidence và conflict lifecycle

- **Validation**: Mọi lệnh gọi MCP được kiểm tra phản hồi qua `EvidenceGateway.call`.
- **Audit & Provenance**: Thu thập `evidence_ref` duy nhất trực tiếp từ MCP server và phát sự kiện `tool_result_consumed`.
- **Conflict Handling**: Khi có sự mâu thuẫn (ví dụ: ngày giao dự kiến vs sự kiện `delivered_late` từ đơn vị vận chuyển), hệ thống ưu tiên nguồn sự kiện từ carrier tracking (`prioritize_carrier_exception_event`) và ghi nhận vào `data_conflicts`.
- **Phạm vi Evidence**: Evidence thu thập theo đúng từng `case_id`, tuyệt đối không dùng chéo giữa các case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / network error | 2 retries | Ghi nhận lỗi mềm, chuyển trạng thái `insufficient_evidence` | `tool_result_consumed` (nếu có ref) |
| Entity not found / ambiguous | 0 retries | Đưa vào `rejected_candidates`, set status `not_found` | `handoff` (status: `not_found`) |
| Source conflict | 0 retries | Áp dụng quy tắc ưu tiên carrier/timeline chính thức | Ghi nhận vào `data_conflicts` |
| Invalid specialist result | 1 retry | Sử dụng giá trị mặc định theo schema an toàn | `task_assigned` |

- **Efficiency**: Chỉ gọi các tool được yêu cầu theo `investigation_scope` (ví dụ: chỉ gọi `get_product_context` khi `include_product_context` là `true`).

## 6. Verification invariants

Trước khi xuất output cuối cùng:
1. `case_id` khớp chính xác với input.
2. `schema_version` là `day09-l3b-output-v2`.
3. Toàn bộ `evidence_refs` phải là các mã `ev_*` thật từ MCP Gateway của case hiện tại.
4. Nếu `case_status == "no_action"`: `recommended_refund_brl` bắt buộc bằng 0 và `refund_lines` rỗng.
5. Nếu `primary_issue == "late_delivery_seller"`: Bên chịu trách nhiệm là `seller` với `party_id` thuộc `late_seller_ids`.
6. Tất cả các mảng ID (`order_ids`, `seller_ids`, `item_ids`) đều là danh sách duy nhất không trùng lặp.

## 7. Reproducibility

- **LLM Model**: `liquid/lfm-2.5-2.6b:free` (qua OpenRouter API, 2.6 tỷ tham số < 10B parameters quy định).
- **Môi trường**: Python 3.11.2, Windows OS (PowerShell).
- **Dependencies**: `httpx2`, `jsonschema`, `mcp`, `pydantic`, `openai`, `python-dotenv`.
- **Deterministic Fallback**: Hệ thống kết hợp deterministic rule engine từ MCP policy và LLM reasoning để đảm bảo 100% tuân thủ JSON schema ngay cả khi có biến động về mạng hoặc rate limit API.
- **Lệnh chạy**: `day09 run` → `day09 validate` → `day09 package --output dist/submission.zip`.

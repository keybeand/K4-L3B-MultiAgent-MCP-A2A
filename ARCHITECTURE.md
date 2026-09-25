# L3B Architecture Record

Tài liệu mô tả chi tiết thiết kế hệ thống Multi-Agent A2A điều tra khiếu nại thương mại điện tử chuẩn hoá theo các quyết định có thể kiểm chứng.

## 1. System overview

Luồng xử lý từ Input Case, giải quyết thực thể (Entity Resolution), điều phối Agent chuyên môn qua MCP Gateway, đến xử lý mâu thuẫn, kiểm tra Verifier và xuất kết quả Trace.

```text
Input → Entity Resolver → Coordinator ⇄ Specialists (Order/Shipment/Payment/Policy)
            │                  │                     │
            │                  ▼                     ▼
            └──────────► MCP Gateway ◄───────────────┘
                               │
                               ▼
                        Conflict Resolver
                               │
                               ▼
                        Verifier Agent ────(Pass?)────► Output JSON & Trace Log
                               │
                          (Fail/Loop)
                               └──────────► Coordinator Retry
```

---

## 2. Agent ownership & Tool Permissions (Least Privilege)

| Actor | Input | Trách nhiệm | Tool permission | Output / Handoff |
| --- | --- | --- | --- | --- |
| **Entity / Customer** | Unstructured claim, Candidate list | Xếp hạng, xác định hoặc reject ứng viên `customer_id`, `order_id` | `search_candidates`, `resolve_entity` | Resolved IDs + Evidence Ref |
| **Coordinator** | Case payload, Candidate IDs | Điều phối ReAct / A2A loop, chuyển giao task, quản lý retry budget | Không trực tiếp gọi tool | Task assignments / Handoff envelope |
| **Order / Product** | Order ID, Case ID | Kiểm tra chi tiết đơn hàng, sản phẩm, cửa hàng | `get_order_details`, `get_product_info` | Order facts + Evidence Ref |
| **Shipment** | Order ID, Tracking Code | Tra cứu hành trình vận chuyển, mốc thời gian giao hàng | `get_shipment_tracking` | Timeline & Shipment facts + Evidence Ref |
| **Payment / Refund** | Order ID, Case ID | Kiểm tra trạng thái thanh toán và lịch sử hoàn tiền | `get_payment_info`, `get_refund_history` | Financial facts + Evidence Ref |
| **Policy** | Claim type, Timestamps | Tra cứu chính sách bảo hành, hoàn tiền áp dụng | `get_refund_policy` | Policy rules + Evidence Ref |
| **Conflict Resolver** | Multi-source facts, Evidence refs | Xử lý dữ liệu mâu thuẫn theo quy tắc ưu tiên bằng chứng | Không trực tiếp gọi tool | Resolved facts / Unresolved flag |
| **Verifier** | Final Output Draft, Trace log | Kiểm tra Schema, Evidence Ownership, Invariants trước finalize | Không trực tiếp gọi tool | Approved / Rejected (Re-evaluate) |

---

## 3. Entity resolution và A2A protocol

- **Candidate Resolution**: So sánh thuộc tính ứng viên với thông tin khiếu nại. Chỉ chấp nhận ứng viên khi `confidence >= 0.8`. Nếu không tìm thấy hoặc kết quả mơ hồ, giữ nguyên mờ và đánh dấu `entity_unresolved`.
- **A2A Message Envelope**: Mọi thông điệp giữa các agent tuân thủ cấu trúc:
  - `case_id`: Mã nhận dạng duy nhất cho từng case.
  - `sender`: Tên Agent gửi (ví dụ: `coordinator`).
  - `receiver`: Tên Agent nhận (ví dụ: `shipment-agent`).
  - `action`: `task_assigned` | `handoff` | `verification_completed`.
  - `payload`: Thông tin truyền tải và danh sách `evidence_refs`.
- **Chống vòng lặp vô hạn**: Giới hạn tối đa 3 lượt tương tác giữa Coordinator và mỗi Specialist Agent. Sau 3 lượt không hoàn thành, buộc chuyển sang Fallback Mode.

---

## 4. Evidence và conflict lifecycle

- **Validation & Ownership**: Mọi phản hồi từ MCP Gateway trả về `data` và `evidence_ref`. `evidence_ref` bắt buộc phải được ghi nhận thông qua sự kiện `tool_result_consumed` với `case_id` tương ứng. Nghiêm cấm dùng lại `evidence_ref` giữa các case.
- **Thứ tự ưu tiên bằng chứng (Source Precedence)**:
  1. Audit Logs & System Tracking (MCP Shipment / MCP Payment) - *Độ tin cậy cao nhất*.
  2. Merchant / Seller response & Invoices - *Độ tin cậy trung bình*.
  3. Customer claim text - *Độ tin cậy cơ sở*.
- **Conflict Representation**: Nếu mâu thuẫn không thể tự giải quyết (ví dụ: hãng vận chuyển xác nhận đã giao nhưng khách bảo không nhận và không có chữ ký), Conflict Resolver đánh dấu `conflict_status = unresolved` và ghi chú rõ ràng trong lý do kết luận.

---

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event / Code |
| --- | ---: | --- | --- |
| **MCP timeout / 5xx** | 2 lần | Trả về thông tin rỗng, ghi nhận `evidence_missing` | `mcp_error_retry` / `mcp_timeout` |
| **Entity ambiguous / Not found** | 1 lần (quét bổ sung) | Chọn Candidate cao điểm nhất nếu > 0.75, ngược lại Reject | `entity_resolution_failed` |
| **Source conflict** | 0 lần retry tool | Nhờ Conflict Resolver xử lý theo Source Precedence | `source_conflict_detected` |
| **Invalid Specialist result** | 1 lần (Coordinator re-assign) | Trực tiếp chuyển sang Verifier để đánh giá thiếu hụt | `specialist_handoff_failed` |

- **Caching & Efficiency Strategy**: Dùng **In-Memory Case Cache** lưu lại kết quả MCP theo cặp `(tool_name, params)` trong phạm vi xử lý 1 case. Không bao giờ gọi trùng 1 MCP tool với tham số giống hệt nhau.

---

## 6. Verification invariants

Trước khi gọi `finalize`:
1. **JSON Schema Check**: Đảm bảo tệp output tuân thủ 100% schema bắt buộc.
2. **Evidence Ownership**: Toàn bộ `evidence_ref` trong output xuất hiện hợp lệ trong audit trace của đúng `case_id` đó.
3. **Financial Consistency**: Tổng số tiền đề xuất hoàn không vượt quá số tiền đơn hàng đã thanh toán (`refund_amount <= paid_amount`).
4. **Timeline Consistency**: Ngày khiếu nại phải sau ngày đặt hàng và ngày giao hàng.
5. **Confidence Bounds**: Điểm tin cậy `confidence` phải nằm trong khoảng `[0.0, 1.0]`.

---

## 7. Reproducibility

- **Target Models**: Phù hợp chạy với các mô hình dưới 10B tham số (ví dụ: `Qwen-2.5-7B`, `Llama-3-8B`, `Gemma-2-9B`) hỗ trợ kịch bản Function Calling & JSON Output.
- **Environment**: Python 3.11+, Pytest, Asyncio.
- **Command Execution**:
  - Chạy toàn bộ case: `day09 run`
  - Kiểm tra hợp lệ: `day09 validate`
  - Đóng gói: `day09 package --output dist/submission.zip`

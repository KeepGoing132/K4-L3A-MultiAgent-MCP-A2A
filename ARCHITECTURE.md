# L3A Architecture Record

Hệ thống điều tra khiếu nại thương mại điện tử đa tác tử (Multi-Agent MCP + A2A) theo chuẩn Day09 L3A V2.

## 1. System overview

Luồng xử lý từ input khiếu nại đến kết quả và observable trace:

```text
Case Input (inputs/<case_id>.json)
  │
  ▼
Coordinator Agent ──────── (case_received)
  │
  ├──► Order/Item Specialist ────► MCP Gateway (get_order, get_items) ────► (tool_result_consumed)
  │        │ (handoff)
  │        ▼
  ├──► Payment Specialist ───────► MCP Gateway (get_payment) ─────────────► (tool_result_consumed)
  │        │ (handoff)
  │        ▼
  ├──► Shipment Specialist ──────► MCP Gateway (get_shipment) ────────────► (tool_result_consumed)
  │        │ (handoff)
  │        ▼
  ├──► Policy Specialist ────────► MCP Gateway (get_policy) ──────────────► (policy_decided)
  │        │ (handoff)
  │        ▼
  └──► Verifier Agent ───────────► Validation & Invariants Check ─────────► (verification_completed)
                                                                                  │
                                                                                  ▼
                                                                           outputs/<case_id>.json
                                                                           traces/trace.jsonl (case_finalized)
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool được phép gọi | Output/handoff |
| --- | --- | --- | --- | --- |
| `coordinator` | `case` object (`case_id`, raw text, entities) | Điều phối quy trình, khởi tạo context, ủy quyền tác vụ (`task_assigned`), quản lý vòng đời | Không gọi MCP tools trực tiếp | Chuyển giao context cho `order_agent` |
| `order_agent` | `order_id`, `context` | Truy vấn dữ liệu đơn hàng, trích xuất mã item, thông tin seller, trạng thái đơn hàng (`delivered`, `canceled`, `unavailable`) | `get_order`, `get_items`, `get_order_items` | `handoff` sang `payment_agent` kèm `order_data`, `item_ids`, `seller_ids` |
| `payment_agent` | `order_id`, `context` | Kiểm tra giao dịch thanh toán, split payment, duplicate charge, số tiền đã thanh toán vs giá trị đơn hàng | `get_payment`, `get_order_payments`, `get_payments` | `handoff` sang `shipment_agent` kèm `payment_data`, `payment_references` |
| `shipment_agent` | `order_id`, `context` | Kiểm tra tiến trình giao hàng, so khớp `delivered_customer_date` vs `estimated_delivery_date` và `shipping_limit_date` | `get_shipment`, `get_order_shipment`, `get_tracking` | `handoff` sang `policy_agent` kèm `shipment_data`, `shipment_ids` |
| `policy_agent` | Toàn bộ findings từ specialists | Đối chiếu quy định sàn TMĐT Olist, phân loại `primary_issue`, tính tiền hoàn BRL, xác định nguyên nhân gốc và bên chịu trách nhiệm | `get_policy` (nếu có) | `handoff` sang `verifier` kèm dự thảo đánh giá và khuyến nghị |
| `verifier` | Dự thảo đánh giá (`context`) | Kiểm tra tính nhất quán (consistency invariants), deduplicate entities, kiểm tra bounds, xác thực JSON Schema | Không gọi MCP tools | Trả output hoàn chỉnh cho Coordinator |

## 3. A2A protocol

* **Correlation:** Tất cả thông điệp và sự kiện được liên kết chặt chẽ qua thuộc tính `case_id`.
* **Trace Lifecycle Order:** Đảm bảo thứ tự xuất hiện sự kiện observable:
  `case_received` $\to$ `task_assigned` $\to$ `tool_result_consumed` $\to$ `handoff` $\to$ `policy_decided` $\to$ `verification_completed` $\to$ `case_finalized`.
* **Tránh lặp vòng:** Luồng chuyển giao một chiều có thứ tự nghiêm ngặt (DAG). Mỗi specialist chỉ thực thi một lần cho mỗi case.
* **Observable Only:** Chỉ emit các mã quyết định (`decision_code`) và metadata cấp hệ thống. Tuyệt đối không đưa prompt, nội dung bí mật hay chuỗi suy luận nội bộ (chain-of-thought) vào trace.

## 4. Evidence lifecycle

* **Discovery:** Gọi `gateway.list_tools()` để khám phá công cụ thực tế có mặt trên server trước khi gọi, không đoán mò tên tool.
* **Audit & Validation:** Mọi response từ MCP Gateway tự động qua schema `day09-mcp-evidence-v1`. Thu thập và lưu giữ `evidence_ref`.
* **Trace Linkage:** Ngay khi nhận kết quả hợp lệ từ MCP, emit sự kiện `tool_result_consumed` với `evidence_refs: [evidence["evidence_ref"]]`.
* **Cách ly theo Case:** Mọi `evidence_ref` được thu thập trong `context` của case tương ứng; không tái sử dụng hay chia sẻ chéo giữa các case khác nhau nhằm tránh hard-gate `cross_scope_evidence_ref` hoặc `unknown_evidence_ref`.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | Tối đa 2 lần retry có exponential backoff | Ghi nhận thiếu bằng chứng, chuyển tiếp các agent khác | `attributes={"status": "timeout"}` |
| Not found / 404 | Không retry | Tiếp tục với context hiện có, đánh dấu `insufficient_evidence` nếu thiếu dữ liệu thiết yếu | `decision_code="ENTITY_NOT_FOUND"` |
| Source conflict | Không retry | Ghi nhận vào `data_conflicts`, ưu tiên dữ liệu từ MCP official evidence so với customer message | `decision_code="CONFLICT_RESOLVED"` |
| Invalid specialist result | Không retry | Verifier tự động điều chỉnh về default an toàn (`needs_investigation` hoặc `unsupported_claim`) | `decision_code="VERIFICATION_ADJUSTED"` |

## 6. Verification invariants

Trước khi xuất kết quả cuối cùng ra `outputs/<case_id>.json`, Verifier kiểm tra các bất biến:
1. **Schema Compliance:** Toàn bộ output thỏa mãn schema `day09-l3a-output-v2.schema.json`.
2. **Entity Deduplication:** Danh sách `order_ids`, `item_ids`, `seller_ids`, `payment_references`, `shipment_ids` là duy nhất, tối đa 20 phần tử.
3. **Evidence Integrity:** Danh sách `evidence_refs` chỉ chứa các mã bắt đầu bằng `ev_...` được trả về từ MCP calls của chính case đó.
4. **Status & Financial Consistency:**
   - Nếu `primary_issue == "unsupported_claim"`: `case_status` bắt buộc là `"no_action"`, `recommended_refund_brl` = 0.0, và `refund_lines` rỗng.
   - Nếu `primary_issue == "insufficient_evidence"`: `case_status` là `"needs_investigation"`.
   - Tổng tiền trong `refund_lines` khớp chính xác với `recommended_refund_brl`.
5. **Confidence Calibration:** Giá trị `confidence` luôn nằm trong đoạn `[0.0, 1.0]`.

## 7. Reproducibility

* **Môi trường thực thi:** Python 3.11+, các thư viện phụ thuộc được pin trong `pyproject.toml` (`httpx2>=2,<3`, `jsonschema>=4.25,<5`, `mcp>=2,<3`).
* **Lệnh chạy:**
  ```bash
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
* **Cấu hình:** Biến môi trường khai báo trong `.env` (`COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`).

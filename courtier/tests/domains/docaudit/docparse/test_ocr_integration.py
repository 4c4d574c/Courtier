"""PaddleOCR / PPStructureV3 integration tests.

TODO: Add OCR pipeline tests:
- Upload a scanned PDF/image and verify PaddleOCR returns structured text
- Test OCR fallback behavior when the OCR endpoint (port 8006) is unreachable
- Verify PPStructureV3 table extraction produces correct markdown tables
- Test OCR language fallback (ch -> en) and explicit language override
- Measure end-to-end latency for a representative scanned document
"""

import json
import sys
from pathlib import Path

import onnx
from onnx import TensorProto, helper


def main() -> None:
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    inputs = [
        helper.make_tensor_value_info(
            "input_ids", TensorProto.INT64, ["batch", "sequence_length"]
        ),
        helper.make_tensor_value_info(
            "attention_mask",
            TensorProto.INT64,
            ["batch", "total_sequence_length"],
        ),
    ]
    outputs = [
        helper.make_tensor_value_info(
            "logits", TensorProto.FLOAT, ["batch", "sequence_length", 2]
        )
    ]
    value = helper.make_tensor("value", TensorProto.FLOAT, [1, 1, 2], [0.0, 1.0])
    graph = helper.make_graph(
        [helper.make_node("Constant", [], ["logits"], value=value)],
        "smoke",
        inputs,
        outputs,
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    onnx.save(model, directory / "model.onnx")
    config = {
        "model": {
            "bos_token_id": 0,
            "context_length": 8,
            "decoder": {
                "session_options": {"provider_options": []},
                "filename": "model.onnx",
                "head_size": 1,
                "hidden_size": 1,
                "inputs": {
                    "input_ids": "input_ids",
                    "attention_mask": "attention_mask",
                    "past_key_names": "past_key_values.%d.key",
                    "past_value_names": "past_key_values.%d.value",
                },
                "outputs": {
                    "logits": "logits",
                    "present_key_names": "present.%d.key",
                    "present_value_names": "present.%d.value",
                },
                "num_attention_heads": 1,
                "num_hidden_layers": 0,
                "num_key_value_heads": 1,
            },
            "eos_token_id": 1,
            "pad_token_id": 0,
            "type": "decoder",
            "vocab_size": 2,
        },
        "search": {"max_length": 8},
    }
    (directory / "genai_config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )


if __name__ == "__main__":
    main()

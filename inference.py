# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: 
"""
import argparse
import json
import os

import torch
from peft import PeftModel
from transformers import (
    AutoModel,
    AutoModelForCausalLM,
    AutoTokenizer,
    BloomForCausalLM,
    BloomTokenizerFast,
    LlamaTokenizer,
    LlamaForCausalLM,
)

from supervised_finetuning import get_conv_template

MODEL_CLASSES = {
    "bloom": (BloomForCausalLM, BloomTokenizerFast),
    "chatglm": (AutoModel, AutoTokenizer),
    "llama": (LlamaForCausalLM, LlamaTokenizer),
    "baichuan": (AutoModelForCausalLM, AutoTokenizer),
    "auto": (AutoModelForCausalLM, AutoTokenizer),
}


class SimpleChatIO:
    def prompt_for_input(self, role) -> str:
        return input(f"{role}: ")

    def prompt_for_output(self, role: str):
        print(f"{role}: ", end="", flush=True)

    def stream_output(self, output_stream):
        print(output_stream, flush=True)
        return output_stream


@torch.inference_mode()
def generate_answer(model, tokenizer, prompt, device, context_len=2048):
    max_new_tokens = 400
    generation_config = dict(
        max_new_tokens=max_new_tokens,
        temperature=0.2,
        top_k=40,
        top_p=0.9,
        do_sample=True,
        num_beams=1,
        repetition_penalty=1.3,
    )
    input_ids = tokenizer(prompt).input_ids
    max_src_len = context_len - max_new_tokens - 8
    input_ids = input_ids[-max_src_len:]
    generation_output = model.generate(
        input_ids=torch.as_tensor([input_ids]).to(device),
        **generation_config,
    )
    output_ids = generation_output[0]
    output = tokenizer.decode(output_ids, skip_special_tokens=False)
    stop_str = tokenizer.eos_token
    l_prompt = len(tokenizer.decode(input_ids, skip_special_tokens=False))
    pos = output.rfind(stop_str, l_prompt)
    if pos != -1:
        output = output[l_prompt:pos]
    else:
        output = output[l_prompt:]
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model_type', default=None, type=str, required=True)
    parser.add_argument('--base_model', default=None, type=str, required=True)
    parser.add_argument('--lora_model', default="", type=str, help="If None, perform inference on the base model")
    parser.add_argument('--tokenizer_path', default=None, type=str)
    parser.add_argument('--template_name', default="vicuna", type=str, help="Prompt template name")
    parser.add_argument('--data_file', default=None, type=str,
                        help="A file that contains instructions (one instruction per line)")
    parser.add_argument('--interactive', action='store_true', help="run in the instruction mode (single-turn)")
    parser.add_argument('--predictions_file', default='./predictions.json', type=str)
    parser.add_argument('--resize_emb', action='store_true', help='Whether to resize model token embeddings')
    args = parser.parse_args()
    print(args)

    load_type = torch.float16
    if torch.cuda.is_available():
        device = torch.device(0)
    else:
        device = torch.device('cpu')
    if args.tokenizer_path is None:
        args.tokenizer_path = args.base_model

    model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    tokenizer = tokenizer_class.from_pretrained(args.tokenizer_path, trust_remote_code=True)
    base_model = model_class.from_pretrained(
        args.base_model,
        load_in_8bit=False,
        torch_dtype=load_type,
        low_cpu_mem_usage=True,
        device_map='auto',
        trust_remote_code=True,
    )

    if args.resize_emb:
        model_vocab_size = base_model.get_input_embeddings().weight.size(0)
        tokenzier_vocab_size = len(tokenizer)
        print(f"Vocab of the base model: {model_vocab_size}")
        print(f"Vocab of the tokenizer: {tokenzier_vocab_size}")
        if model_vocab_size != tokenzier_vocab_size:
            print("Resize model embeddings to fit tokenizer")
            base_model.resize_token_embeddings(tokenzier_vocab_size)

    if args.lora_model:
        model = PeftModel.from_pretrained(base_model, args.lora_model, torch_dtype=load_type, device_map='auto')
        print("Loaded lora model")
    else:
        model = base_model
    print(tokenizer)
    # test data
    if args.data_file is None:
        examples = ["介绍下北京", "乙肝和丙肝的区别？"]
    else:
        with open(args.data_file, 'r') as f:
            examples = [l.strip() for l in f.readlines()]
        print("first 10 examples:")
        for example in examples[:10]:
            print(example)
    model.eval()

    chatio = SimpleChatIO()
    with torch.no_grad():
        if args.interactive:
            conv = get_conv_template(args.template_name)
            print("Start inference with interactive mode.")

            while True:
                try:
                    inp = chatio.prompt_for_input(conv.roles[0])
                except EOFError:
                    inp = ""
                if not inp:
                    print("exit...")
                    break

                conv.append_message(conv.roles[0], inp)
                conv.append_message(conv.roles[1], '')

                prompt = conv.get_prompt()
                chatio.prompt_for_output(conv.roles[1])
                output = generate_answer(model, tokenizer, prompt, device)
                outputs = chatio.stream_output(output)
                # NOTE: strip is important to align with the training data.
                conv.messages[-1][-1] = outputs.strip()
                # print("\n", {"prompt": prompt, "outputs": outputs}, "\n")
        else:
            print("Start inference.")
            results = []
            for index, example in enumerate(examples):
                conv = get_conv_template(args.template_name)
                conv.append_message(conv.roles[0], example)
                conv.append_message(conv.roles[1], '')

                prompt = conv.get_prompt()
                response = generate_answer(model, tokenizer, prompt, device)
                print(f"======={index}=======")
                print(f"Input: {example}\n")
                print(f"Output: {response}\n")
                results.append({"Input": prompt, "Output": response})

            dirname = os.path.dirname(args.predictions_file)
            os.makedirs(dirname, exist_ok=True)
            with open(args.predictions_file, 'w') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()

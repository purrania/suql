from transformers import AutoModel, AutoTokenizer
import pymongo
from typing import List
import os
from flask import request, Flask
import time
import torch
from utils import linearize, chunk_text
import json
from tqdm import tqdm
import hashlib
from functools import reduce
import operator
from utils import num_tokens_from_string

cuda_ok = torch.cuda.is_available()
model = AutoModel.from_pretrained("OpenMatch/cocodr-base-msmarco")
if cuda_ok:
    device = torch.device("cuda")
    model = model.to(device)
    
tokenizer = AutoTokenizer.from_pretrained("OpenMatch/cocodr-base-msmarco")

mongo = os.environ.get('COSMOS_CONNECTION_STRING')
client = pymongo.MongoClient(mongo)
db = client['yelpbot']
collection = db['yelp_data']
schematized = db['schematized']
cache_db = client['free_text_cache']['hash_to_embeddings']

# Set the server address
host = "127.0.0.1"
port = 8500
review_server_address = 'http://{}:{}'.format(host, port)
app = Flask(__name__)


def compute_sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()

@app.route('/answer', methods=['POST'])
def answer():
    from prompt_continuation import llm_generate
    data = request.get_json()
    # print("/answer receieved request {}".format(data))
        
    # input params in this `data`    
    # data["text"] : text to QA upon
    # data["question"] : question to answer

    if "text" not in data or "question" not in data:
        return None
    
    if not data["text"]:
        return {
            "result": "no information"
        }
    
    text_res = []
    if isinstance(data["text"], list):
        documents = _compute_single_embedding_with_mapping(data["text"], data["question"], top=5)
        for i in documents:
            if num_tokens_from_string('\n'.join(text_res + [i])) < 3800:
                text_res.append(i)
            else:
                break
    else:
        text_res = [data["text"]]
        
    type_prompt = ""
    if "type_prompt" in data:
        if data["type_prompt"] == "date":
            type_prompt = f" Output in date format, for instance 2001-09-28."
        if data["type_prompt"] == "int4":
            type_prompt = f" Output an integer."
    
    continuation, _ = llm_generate(
        'prompts/review_qa.prompt',
        {'reviews': text_res, 'question': data["question"], "type_prompt": type_prompt},
        engine='gpt-3.5-turbo-0613',
        max_tokens=200,
        temperature=0.0,
        stop_tokens=['\n'],
        postprocess=False
    )
    
    res = {
        "result" : continuation
    }
    print(res)
    return res

@app.route('/summary', methods=['POST'])
def summary():
    from prompt_continuation import llm_generate
    data = request.get_json()
    # print("/answer receieved request {}".format(data))
        
    # input params in this `data`    
    # data["text"] : text to QA upon
    # (optional) data["focus"] : focus of summary

    if "text" not in data:
        return None
    
    if not data["text"]:
        return {
            "result": "no information"
        }
    
    text_res = []
    if isinstance(data["text"], list):
        for i in data["text"]:
            if num_tokens_from_string('\n'.join(text_res + [i])) < 3800:
                text_res.append(i)
            else:
                break
    else:
        text_res = [data["text"]]
    
    continuation, _ = llm_generate(
        'prompts/review_qa.prompt',
        {'reviews': text_res, 'question': "what is the summary of this document?"},
        engine='gpt-3.5-turbo-0613',
        max_tokens=200,
        temperature=0.0,
        stop_tokens=['\n'],
        postprocess=False,
    )
    
    res = {
        "result" : continuation
    }
    print(res)
    return res

def _compute_single_embedding(documents, chunking_param=15, safe_assume_exists = False):
    documents_hashes = list(map(compute_sha256, documents))
    cache_results = list(cache_db.find({"_id": {"$in": documents_hashes}}))
    
    existing_embeddings = reduce(operator.add, map(lambda x: x["embeddings"], cache_results)) if cache_results else []
    existing_embeddings = torch.tensor(existing_embeddings, device=device)
    
    if safe_assume_exists:
        return existing_embeddings
    
    index_dict = {hash: index for index, hash in enumerate(documents_hashes)}

    for doc in cache_results:
        index_dict.pop(doc["_id"], None)
    
    missing_hashes_with_indices = list(index_dict.items())
    for missing_hash, missing_index in missing_hashes_with_indices:
        chunked_documents = chunk_text(documents[missing_index], k=chunking_param, use_spacy=True)
        inputs = tokenizer(chunked_documents, padding=True, truncation=True, return_tensors="pt").to(device)
        with torch.no_grad():  # Disables gradient calculation to save memory
            embeddings = model(**inputs, output_hidden_states=True, return_dict=True).hidden_states[-1][:, :1].squeeze(1).to(device)  # the embedding of the [CLS] token after the final layer
        existing_embeddings = torch.cat((existing_embeddings, embeddings), dim=0)
        cache_db.insert_one({
            "_id": missing_hash,
            "embeddings": embeddings.tolist()
        })
    
    return existing_embeddings

def _compute_single_embedding_with_mapping(documents, question, chunking_param=15, top=1):
    existing_embeddings = None
    embedding2document = {}
    embedding_counter = 0
    for index, document in enumerate(documents):
        document_hash = compute_sha256(document)
        cache_results = cache_db.find_one({"_id": document_hash})
        
        if not cache_results:
            chunked_documents = chunk_text(document, k=chunking_param, use_spacy=True)
            inputs = tokenizer(chunked_documents, padding=True, truncation=True, return_tensors="pt").to(device)
            embeddings = model(**inputs, output_hidden_states=True, return_dict=True).hidden_states[-1][:, :1].squeeze(1).to(device)  # the embedding of the [CLS] token after the final layer
            cache_db.insert_one({
                "_id": document_hash,
                "embeddings": embeddings.tolist()
            })
        else:
            embeddings = torch.tensor(cache_results["embeddings"], device=device)
        
        if existing_embeddings is None:
            existing_embeddings = embeddings
        else:
            existing_embeddings = torch.cat((existing_embeddings, embeddings), dim=0)
            
        for embedding_index in range(embedding_counter, embedding_counter + embeddings.size()[0]):
            embedding2document[embedding_index] = index
        embedding_counter += embeddings.size()[0]
        
    question_embedding = _compute_single_embedding([question], chunking_param=chunking_param)[0]
    dot_products = torch.mv(existing_embeddings, question_embedding)
    _, indices_max = torch.topk(dot_products, existing_embeddings.size()[0])
    
    res = []
    for index in indices_max:
        index = int(index.item())
        if documents[embedding2document[index]] not in res:
            res.append(documents[embedding2document[index]])
        if len(res) >= top:
            break
        
    torch.cuda.empty_cache()
    return res

if __name__ == "__main__":
    app.run(host=host, port=port)

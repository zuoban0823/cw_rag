import os
import shutil
from typing import List, Optional
#以此为准，替换原来的一行
from fastapi import FastAPI, UploadFile, File, HTTPException, Header, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# --- LangChain 组件 ---
from langchain_openai import ChatOpenAI
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_community.document_loaders import PyPDFLoader, Docx2txtLoader, UnstructuredMarkdownLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough, RunnableLambda
from langchain_community.tools import DuckDuckGoSearchRun

# 初始化 FastAPI
app = FastAPI(title="云财务客服小助手")
templates = Jinja2Templates(directory="templates")

# --- 全局变量 (模拟会话存储，生产环境建议用 Redis 或数据库) ---
# 存储向量数据库实例
vector_store = None
# 存储当前的 API Key (仅用于简单的单用户演示场景)
current_api_key = None


# 定义请求体
class ChatRequest(BaseModel):
    question: str


# --- 核心逻辑函数 ---

def get_llm(api_key: str):
    """根据用户 Key 获取 LLM 实例"""
    return ChatOpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",  # 显式指定阿里云 endpoint
        model="qwen-plus",
        temperature=0.1  # 财务场景需要严谨
    )


def get_embeddings(api_key: str):
    """获取 Embedding 模型"""
    return DashScopeEmbeddings(dashscope_api_key=api_key)


def load_file(file_path: str, file_extension: str):
    """根据文件后缀选择加载器"""
    if file_extension == ".pdf":
        return PyPDFLoader(file_path).load()
    elif file_extension == ".docx":
        return Docx2txtLoader(file_path).load()
    elif file_extension == ".md":
        return UnstructuredMarkdownLoader(file_path).load()
    else:
        raise ValueError(f"不支持的文件格式: {file_extension}")


# --- 路由定义 ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/upload")
async def upload_files(
        files: List[UploadFile] = File(...),
        x_api_key: str = Header(..., alias="X-API-Key")
):
    """处理文件上传并建立向量库"""
    global vector_store, current_api_key

    if not x_api_key:
        raise HTTPException(status_code=401, detail="未提供 API Key")

    current_api_key = x_api_key

    # 创建临时目录保存文件
    temp_dir = "temp_files"
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir)

    all_documents = []

    try:
        for file in files:
            file_ext = os.path.splitext(file.filename)[1].lower()
            file_path = os.path.join(temp_dir, file.filename)

            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            # 加载并解析
            try:
                docs = load_file(file_path, file_ext)
                all_documents.extend(docs)
            except Exception as e:
                print(f"文件 {file.filename} 解析失败: {e}")
                continue

        if not all_documents:
            return {"message": "没有成功解析任何文件，请检查文件格式。"}

        # 分割文本
        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
        split_docs = splitter.split_documents(all_documents)

        # 重建向量库 (使用用户的 Key)
        # 注意：Chroma 在这里是内存模式，重启或重新上传会覆盖
        vector_store = Chroma.from_documents(
            documents=split_docs,
            embedding=get_embeddings(x_api_key)
        )

        return {"message": f"成功解析并学习了 {len(files)} 个文件，知识库已更新。"}

    finally:
        # 清理临时文件
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)


@app.post("/chat")
async def chat_endpoint(
        request: ChatRequest,
        x_api_key: str = Header(..., alias="X-API-Key")
):
    global vector_store

    if not x_api_key:
        raise HTTPException(status_code=401, detail="请先设置 API Key")

    question = request.question
    llm = get_llm(x_api_key)

    # 1. 尝试从向量库检索 (RAG)
    context = ""
    source = "knowledge_base"

    if vector_store:
        retriever = vector_store.as_retriever(search_kwargs={"k": 3})
        docs = retriever.invoke(question)
        if docs:
            context = "\n\n".join([doc.page_content for doc in docs])

    # 2. 如果本地没有足够信息，进行联网搜索 (Web Search)
    # 简单的判断逻辑：如果 context 为空，或者我们让 LLM 自行判断（这里为了速度和明确性，若 context 空则搜）
    # 也可以结合使用：将搜索结果作为补充
    if not context:
        print("本地知识库无结果，正在联网搜索...")
        source = "web_search"
        search = DuckDuckGoSearchRun()
        try:
            # 限制搜索关键词，强制关联财务
            search_query = f"{question} 财务软件"
            search_result = search.invoke(search_query)
            context = search_result
        except Exception as e:
            print(f"搜索失败: {e}")
            context = ""

    # 3. 最终生成的 Prompt
    # 核心指令：必须关于财务，必须真实，如果都没有则返回特定话术
    template = """
    你自称为“云财务客服小助手”。
    你的任务是基于以下提供的【上下文信息】回答用户的【问题】。

    严格遵守以下规则：
    1. **领域限制**：回答必须与“财务”、“财务软件”、“税务”或“会计处理”高度相关。
    2. **真实性**：只能基于上下文回答，不要编造事实。
    3. **兜底回复**：如果上下文为空，或者上下文内容无法回答该问题，或者问题与财务领域完全无关，
       请**严格且仅**返回这句话：“抱歉，知识库正在学习更新中，暂时无法回答您的问题。”
    4. **语气**：专业、严谨、客气。

    ---
    【上下文信息】({source}):
    {context}
    ---
    【用户问题】:
    {question}
    """

    prompt = ChatPromptTemplate.from_template(template)

    chain = (
            {"context": lambda x: context, "source": lambda x: source, "question": RunnablePassthrough()}
            | prompt
            | llm
            | StrOutputParser()
    )

    response = chain.invoke(question)

    return {"answer": response, "source": source}


if __name__ == "__main__":
    import uvicorn
    from starlette.requests import Request  # 修复引用

    uvicorn.run(app, host="localhost", port=8000)
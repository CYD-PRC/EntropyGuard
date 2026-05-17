from flask import Flask, render_template, request, jsonify
import json
import os
import random
import datetime

app = Flask(__name__)

# GAIA 知识库
GAIA_KNOWLEDGE = {
    "什么是GAIA": "GAIA（General AI Assistant）是通用人工智能助手的简称，代表新一代具备自主推理、多模态交互和持续学习能力的AI系统。",
    "GAIA的核心能力": [
        "🧠 自主推理与决策",
        "💬 多轮对话理解",
        "📊 数据分析与可视化",
        "🔧 工具调用与自动化",
        "🌐 多模态信息处理",
        "🔄 持续学习与进化"
    ],
    "GAIA架构层次": {
        "感知层": "文本、图像、语音等多模态输入处理",
        "认知层": "知识图谱、推理引擎、记忆管理",
        "执行层": "工具调用、API集成、代码执行",
        "进化层": "反馈学习、参数优化、能力扩展"
    },
    "GAIA vs 传统AI": {
        "传统AI": "单一任务、规则驱动、静态知识、被动响应",
        "GAIA": "多任务泛化、目标驱动、动态学习、主动建议"
    }
}

conversation_history = []

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    user_msg = data.get('message', '').strip()
    
    if not user_msg:
        return jsonify({'reply': '请告诉我你想了解什么关于GAIA的内容？'})
    
    conversation_history.append({'role': 'user', 'content': user_msg})
    
    # 智能匹配回复
    reply = generate_reply(user_msg)
    conversation_history.append({'role': 'assistant', 'content': reply})
    
    return jsonify({'reply': reply})

def generate_reply(user_msg):
    msg_lower = user_msg.lower()
    
    # 匹配已知知识
    for key, value in GAIA_KNOWLEDGE.items():
        if key.lower() in msg_lower:
            if isinstance(value, dict):
                return format_dict(value)
            elif isinstance(value, list):
                return '\n'.join(value)
            return value
    
    # 关键词匹配
    if '能力' in msg_lower or '能做什么' in msg_lower:
        return "GAIA的核心能力包括：\n" + '\n'.join(GAIA_KNOWLEDGE['GAIA的核心能力'])
    
    if '架构' in msg_lower or '结构' in msg_lower:
        return "GAIA的四层架构：\n" + format_dict(GAIA_KNOWLEDGE['GAIA架构层次'])
    
    if '对比' in msg_lower or '区别' in msg_lower or 'vs' in msg_lower:
        return "GAIA vs 传统AI：\n" + format_dict(GAIA_KNOWLEDGE['GAIA vs 传统AI'])
    
    if '你好' in msg_lower or 'hello' in msg_lower:
        return "你好！我是GAIA智能助手，可以为你介绍GAIA的概念、能力、架构等相关知识。请问你想了解什么？"
    
    # 默认回复
    greetings = [
        f"关于「{user_msg}」，GAIA认为这是一个值得深入探讨的话题。让我从几个维度来分析...",
        f"你提到了{user_msg}，这与GAIA的核心理念密切相关。GAIA通过持续学习来理解复杂概念。",
        f"有趣的问题！{user_msg}在GAIA的认知框架中可以被分解为多个子问题来逐步解答。",
        f"GAIA对「{user_msg}」的理解是：任何复杂问题都可以通过分层推理来解决。"
    ]
    return random.choice(greetings)

def format_dict(d, indent=0):
    result = []
    for k, v in d.items():
        prefix = '  ' * indent
        result.append(f"{prefix}• **{k}**: {v}")
    return '\n'.join(result)

@app.route('/status')
def status():
    return jsonify({
        'status': 'running',
        'model': 'GAIA v1.0',
        'knowledge_entries': len(GAIA_KNOWLEDGE),
        'conversations': len(conversation_history),
        'timestamp': datetime.datetime.now().isoformat()
    })

@app.route('/reset', methods=['POST'])
def reset():
    conversation_history.clear()
    return jsonify({'reply': '对话已重置，GAIA已准备好重新开始！'})

if __name__ == '__main__':
    os.makedirs('templates', exist_ok=True)
    print("🚀 GAIA Assistant 启动中...")
    app.run(host='0.0.0.0', port=5000, debug=True)

FROM --platform=amd64 kali

RUN useradd -m -s /bin/bash kali && \
    echo "kali ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
USER kali

# apt 安装常用工具和安全工具
RUN sudo apt update && \
    sudo apt install -y bsdextrautils npm jq iputils-ping sshpass ncat rlwrap \
    bloodyad coercer enum4linux-ng pwncat chisel-common-binaries \
    dirsearch yq krb5-user gitleaks naabu nikto netexec adb && \
    sudo apt clean && \
    sudo rm -rf /var/lib/apt/lists/* && \
    pip3 install -i https://mirrors.ustc.edu.cn/pypi/simple/ pwntools pymongo --break-system-packages && \
    sudo npm install -g @playwright/cli@latest && \
    cd /tmp && playwright-cli install

# 补充安全工具
RUN cd /tmp && \
    wget -q "https://ghfast.top/https://github.com/projectdiscovery/katana/releases/download/v1.5.0/katana_1.5.0_linux_amd64.zip" -O katana.zip && \
    wget -q "https://ghfast.top/https://github.com/projectdiscovery/nuclei/releases/download/v3.7.1/nuclei_3.7.1_linux_amd64.zip" -O nuclei.zip && \
    wget -q "https://ghfast.top/https://github.com/hahwul/dalfox/releases/download/v2.12.0/dalfox-linux-amd64.tar.gz" -O dalfox.tar.gz && \
    wget -q "https://ghfast.top/https://github.com/BishopFox/cloudfox/releases/download/v2.0.1/cloudfox-linux-amd64.zip" -O cloudfox.zip && \
    wget -q "https://ghfast.top/https://github.com/ropnop/kerbrute/releases/download/v1.0.3/kerbrute_linux_amd64" -O kerbrute && \
    git clone --depth 1 "https://ghfast.top/https://github.com/projectdiscovery/nuclei-templates.git" /home/kali/.local/nuclei-templates && \
    unzip -q katana.zip katana && sudo mv katana /usr/local/bin/ && \
    unzip -q nuclei.zip nuclei && sudo mv nuclei /usr/local/bin/ && \
    tar -xzf dalfox.tar.gz && sudo mv dalfox-linux-amd64 /usr/local/bin/dalfox && \
    unzip -q cloudfox.zip cloudfox/cloudfox && sudo mv cloudfox/cloudfox /usr/local/bin/ && \
    sudo cp kerbrute /usr/local/bin/ && \
    sudo rm -rf /tmp/*

# 知识库
RUN mkdir -p /home/kali/knowledges && cd /home/kali/knowledges && \
    git clone --depth=1 https://ghfast.top/https://github.com/swisskyrepo/PayloadsAllTheThings.git && \
    git clone --depth=1 https://ghfast.top/https://github.com/swisskyrepo/InternalAllTheThings.git && \
    git clone --depth=1 https://ghfast.top/https://github.com/HackTricks-wiki/hacktricks.git && \
    git clone --depth=1 https://ghfast.top/https://github.com/HackTricks-wiki/hacktricks-cloud.git

# 安装 codex 和 claude-code
RUN sudo npm install -g @openai/codex@0.118.0
RUN sudo npm install -g @anthropic-ai/claude-code@2.1.98


# 下载一些工具
RUN mkdir -p /home/kali/tools && cd /home/kali/tools && \
    wget -q "https://gh-proxy.org/https://github.com/frohoff/ysoserial/releases/download/v0.0.6/ysoserial-all.jar" -O ysoserial.jar && \
    git clone --depth=1 https://ghfast.top/https://github.com/ticarpi/jwt_tool.git && \
    cd jwt_tool && pip3 install -i https://mirrors.ustc.edu.cn/pypi/simple/ -r requirements.txt --break-system-packages
    

# 下载一些 PoC
RUN mkdir -p /home/kali/pocs && cd /home/kali/pocs && \
    git clone --depth=1 https://gh-proxy.org/https://github.com/iSee857/CVE-PoC.git && \
    git clone --depth=1 https://gh-proxy.org/https://github.com/zhzyker/exphub.git && \
    git clone --depth=1 https://gh-proxy.org/https://github.com/passwa11/2023Hvv_.git && \
    git clone --depth=1 https://gh-proxy.org/https://github.com/Threekiii/Awesome-POC.git

RUN cd /home/kali/pocs && \
    git clone --depth=1 https://gh-proxy.org/https://github.com/vulhub/vulhub.git

RUN cd /home/kali/tools && \
    git clone --depth=1 https://gh-proxy.org/https://github.com/IOActive/jdwp-shellifier.git

# 初始化 nuclei 配置
RUN nuclei -ut
    
RUN sudo npm install -g @anthropic-ai/claude-code@2.1.111

# 运行时配置
RUN sudo chmod +x /usr/local/bin/kerbrute
RUN mkdir -p /home/kali/.config/nuclei && \
    echo >> /home/kali/.config/nuclei/config.yaml && \
    echo "disable-update-check: true" >> /home/kali/.config/nuclei/config.yaml && \
    echo "update-template-dir: /home/kali/.local/nuclei-templates" >> /home/kali/.config/nuclei/config.yaml && \
    rm -rf /home/kali/nuclei-templates
RUN sudo rm -rf /usr/lib/llvm-*/build && \
    sudo chmod o+rx /usr/lib/mysql/plugin/auth_pam_tool_dir /etc/ssl/private
RUN printf '%s\n' 'PS1="${PS1%\\n*} \[\033[0;36m\]\D{%y-%m-%d} - \t\[\033[;32m\]\n${PS1##*\\n}"' >> /home/kali/.bashrc && \
    mkdir -p /home/kali/workspace && cd /home/kali/workspace && git init
ENV TZ=Asia/Shanghai
ENV PLAYWRIGHT_MCP_BROWSER=chromium
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENV PYTHONUNBUFFERED=1

# 配置 agent
COPY ./.agents /home/kali/workspace/.agents
COPY ./.agents /home/kali/workspace/.claude
COPY ./AGENTS.md /home/kali/workspace/AGENTS.md
COPY ./AGENTS.md /home/kali/workspace/CLAUDE.md

WORKDIR /home/kali/workspace
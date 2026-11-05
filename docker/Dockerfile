# 该文件是作为 docker 镜像的构建文件，主要用于在容器中运行 sshd 服务。
# 后续会考虑将接收端内置 这是最初版的文件 作为参考
FROM ubuntu:22.04

# 安装 openssh-server 并清理缓存
RUN apt-get update && \
    apt-get install -y openssh-server && \
    rm -rf /var/lib/apt/lists/*

# 创建 sshd 运行时目录（权限必须为 root:root 755）
RUN mkdir -p /var/run/sshd && \
    chown root:root /var/run/sshd && \
    chmod 755 /var/run/sshd

# 设置 root 密码
RUN echo 'root:123456' | chpasswd

# 允许 root 
RUN echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config && \
    echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config

# 关键：前台运行 sshd，-D 表示不进入后台，-e 将日志输出到 stderr 便于 docker logs 查看
CMD ["/usr/sbin/sshd", "-D", "-e"]
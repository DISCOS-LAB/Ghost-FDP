# MySQL 8.4 source build + Sysbench

Prefix: `/usr/local/mysql`. Data, socket, and tmpdir live on the NVMe mount `/mnt/nvme`.

## 1. Build dependencies

```bash
sudo apt update
sudo apt install -y build-essential cmake libncurses5-dev libssl-dev \
  pkg-config bison libtirpc-dev
```

## 2. Download MySQL 8.4 source

```bash
wget https://dev.mysql.com/get/Downloads/MySQL-8.4/mysql-8.4.0.tar.gz
tar -zxvf mysql-8.4.0.tar.gz
cd mysql-8.4.0
```

## 3. cmake / make / install

```bash
mkdir build && cd build
cmake .. \
  -DDOWNLOAD_BOOST=1 \
  -DWITH_BOOST=../boost \
  -DCMAKE_INSTALL_PREFIX=/usr/local/mysql \
  -DWITH_SSL=system \
  -DDEFAULT_CHARSET=utf8mb4 \
  -DDEFAULT_COLLATION=utf8mb4_general_ci

make -j$(nproc)
sudo make install
```

## 4. Create mysql user/group

Skip if the `mysql` account already exists.

```bash
sudo groupadd mysql
sudo useradd -r -g mysql -s /bin/false mysql
```

## 5. Data / log / tmp directories

```bash
sudo mkdir -p /mnt/nvme/mysql_data/data
sudo mkdir -p /mnt/nvme/mysql_data/log
sudo mkdir -p /mnt/nvme/mysql_tmp

sudo chown -R mysql:mysql /mnt/nvme/mysql_data /mnt/nvme/mysql_tmp
sudo chmod -R 750 /mnt/nvme/mysql_data /mnt/nvme/mysql_tmp
```

## 6. `/etc/my.cnf`

```bash
sudo tee /etc/my.cnf >/dev/null <<'EOF'
[mysqld]
basedir = /usr/local/mysql
datadir = /mnt/nvme/mysql_data/data
tmpdir = /mnt/nvme/mysql_tmp
innodb_tmpdir = /mnt/nvme/mysql_tmp

socket = /mnt/nvme/mysql_data/mysql.sock
pid-file = /mnt/nvme/mysql_data/mysqld.pid
log-error = /mnt/nvme/mysql_data/log/error.log

max_prepared_stmt_count = 1000000
innodb_redo_log_capacity = 17179869184   # 16GiB

[client]
socket = /mnt/nvme/mysql_data/mysql.sock
EOF
```

## 7. Initialize + clear root password

```bash
sudo /usr/local/mysql/bin/mysqld --initialize --user=mysql
# Temporary password is in error.log
/usr/local/mysql/bin/mysql -u root -p
```

```sql
ALTER USER 'root'@'localhost' IDENTIFIED BY '';
```

## 8. systemd unit

```bash
sudo tee /etc/systemd/system/mysqld.service >/dev/null <<'EOF'
[Unit]
Description=MySQL 8.4 Custom Build
After=network.target

[Service]
User=mysql
Group=mysql
ExecStart=/usr/local/mysql/bin/mysqld
LimitNOFILE=100000

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now mysqld
```

## Reset & re-init

```bash
sudo systemctl stop mysqld

sudo mkdir -p /mnt/nvme/mysql_data/data
sudo mkdir -p /mnt/nvme/mysql_data/log
sudo mkdir -p /mnt/nvme/mysql_tmp
sudo chown -R mysql:mysql /mnt/nvme/mysql_data /mnt/nvme/mysql_tmp
sudo chmod -R 750 /mnt/nvme/mysql_data /mnt/nvme/mysql_tmp

sudo /usr/local/mysql/bin/mysqld --initialize --user=mysql
sudo systemctl start mysqld
```

Temporary password is in `error.log`. Then:

```sql
ALTER USER 'root'@'localhost' IDENTIFIED BY '';
```

## Sysbench

```bash
sudo apt install -y sysbench
/usr/local/mysql/bin/mysql -u root -e "CREATE DATABASE sbtest;"
```

**prepare**

```bash
sysbench oltp_read_write \
  --db-driver=mysql \
  --mysql-socket=/mnt/nvme/mysql_data/mysql.sock \
  --mysql-user=root \
  --mysql-db=sbtest \
  --tables=10 \
  --table-size=1000000 \
  --threads=64 \
  prepare
```

**run** (write-only, 60s)

```bash
sysbench oltp_write_only \
  --db-driver=mysql \
  --mysql-socket=/mnt/nvme/mysql_data/mysql.sock \
  --mysql-user=root \
  --mysql-db=sbtest \
  --tables=10 \
  --table-size=1000000 \
  --threads=64 \
  --report-interval=10 \
  --time=60 \
  run
```

## Sysbench TPC-C

```bash
git clone https://github.com/Percona-Lab/sysbench-tpcc.git
cd sysbench-tpcc
/usr/local/mysql/bin/mysql -u root -e "CREATE DATABASE tpcc;"
```

**prepare**

```bash
sysbench ./tpcc.lua \
  --mysql-socket=/mnt/nvme/mysql_data/mysql.sock \
  --mysql-user=root \
  --mysql-db=tpcc \
  --tables=1 \
  --scale=800 \
  --threads=64 \
  prepare
```

**run**

```bash
sysbench ./tpcc.lua \
  --mysql-socket=/mnt/nvme/mysql_data/mysql.sock \
  --mysql-user=root \
  --mysql-db=tpcc \
  --tables=1 \
  --scale=800 \
  --threads=64 \
  --time=120 \
  --report-interval=10 \
  run
```

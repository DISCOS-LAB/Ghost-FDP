# PostgreSQL 18 source build

Branch: **REL_18_STABLE**. Prefix: `/usr/local/pgsql`. Data, socket, and logs live on the NVMe mount `/mnt/nvme`.

## 1. Build dependencies

```bash
sudo apt update
sudo apt install -y build-essential git libreadline-dev zlib1g-dev \
  libicu-dev libssl-dev pkgconf flex bison
```

## 2. Download PostgreSQL 18 source

`git.postgresql.org` often drops the connection. Use the GitHub mirror, tip of the branch only:

```bash
git clone --depth 1 --branch REL_18_STABLE https://github.com/postgres/postgres.git postgresql
cd postgresql
```

## 3. configure / make / install

```bash
mkdir build && cd build
../configure --prefix=/usr/local/pgsql --enable-debug --enable-depend
make -j$(nproc)
sudo make install
```

## 4. Create postgres user/group

Skip if the `postgres` account already exists.

```bash
sudo groupadd postgres
sudo useradd -r -g postgres -s /bin/false postgres
```

## 5. Data / log directories

```bash
sudo mkdir -p /mnt/nvme/pgsql_data/data
sudo mkdir -p /mnt/nvme/pgsql_data/log
sudo mkdir -p /mnt/nvme/pgsql_data/conf.d

sudo chown -R postgres:postgres /mnt/nvme/pgsql_data
sudo chmod -R 750 /mnt/nvme/pgsql_data
```

## 6. Extra config (`pgsql.conf`)

`initdb` rewrites `postgresql.conf` inside the data directory, so keep extra settings in `conf.d` and `include` them.

```bash
sudo tee /mnt/nvme/pgsql_data/conf.d/pgsql.conf >/dev/null <<'EOF'
listen_addresses = 'localhost'
unix_socket_directories = '/mnt/nvme/pgsql_data'
max_connections = 256

logging_collector = on
log_directory = '/mnt/nvme/pgsql_data/log'
log_filename = 'postgresql.log'

shared_buffers = 1GB
debug_io_direct = 'data'
checkpoint_timeout = 300s
checkpoint_completion_target = 0.9
max_wal_size = 1GB
min_wal_size = 512MB
wal_recycle = on
full_page_writes = on
fsync = on
synchronous_commit = on
autovacuum = off
log_checkpoints = on
EOF

sudo chown postgres:postgres /mnt/nvme/pgsql_data/conf.d/pgsql.conf
sudo chmod 640 /mnt/nvme/pgsql_data/conf.d/pgsql.conf
```

Clients use `-h /mnt/nvme/pgsql_data` (socket: `/mnt/nvme/pgsql_data/.s.PGSQL.5432`).

## 7. Initialize

```bash
sudo -u postgres /usr/local/pgsql/bin/initdb -D /mnt/nvme/pgsql_data/data

echo "include = '/mnt/nvme/pgsql_data/conf.d/pgsql.conf'" \
  | sudo tee -a /mnt/nvme/pgsql_data/data/postgresql.conf
```

## 8. systemd unit

```bash
sudo tee /etc/systemd/system/postgresql.service >/dev/null <<'EOF'
[Unit]
Description=PostgreSQL 18 Custom Build
After=network.target

[Service]
Type=simple
User=postgres
Group=postgres
ExecStart=/usr/local/pgsql/bin/postgres -D /mnt/nvme/pgsql_data/data
ExecReload=/bin/kill -HUP $MAINPID
KillMode=mixed
KillSignal=SIGINT
TimeoutStopSec=infinity
LimitNOFILE=100000

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now postgresql
```

```bash
sudo -u postgres /usr/local/pgsql/bin/psql \
  -h /mnt/nvme/pgsql_data \
  -c 'SELECT version();'
```

## Rebuild after source changes

From `postgresql/build`:

```bash
make -j$(nproc)
sudo make install
sudo systemctl restart postgresql
```

## Reset & re-init

```bash
sudo systemctl stop postgresql

sudo rm -rf /mnt/nvme/pgsql_data/data
sudo mkdir -p /mnt/nvme/pgsql_data/{data,log,conf.d}
sudo chown -R postgres:postgres /mnt/nvme/pgsql_data
sudo chmod -R 750 /mnt/nvme/pgsql_data

sudo -u postgres /usr/local/pgsql/bin/initdb -D /mnt/nvme/pgsql_data/data
echo "include = '/mnt/nvme/pgsql_data/conf.d/pgsql.conf'" \
  | sudo tee -a /mnt/nvme/pgsql_data/data/postgresql.conf

sudo systemctl start postgresql
```

Re-copy `pgsql.conf` first if `conf.d` was wiped (section 6).

## HammerDB TPROC-C

```bash
wget https://github.com/TPC-Council/HammerDB/releases/download/v6.0/HammerDB-6.0-Prod-Lin-UBU24.tar.gz
tar -xzf HammerDB-6.0-Prod-Lin-UBU24.tar.gz
cd HammerDB-6.0
```

Connect as `postgres` / `postgres` to `localhost:5432`, database `tpcc` (user `tpcc`).

```bash
./hammerdbcli auto tproc_c_build.tcl
./hammerdbcli auto tproc_c_run.tcl
```

Interactive CLI: `./hammerdbcli`, then `exit`.

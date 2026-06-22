# 演示数据库

`task_records.demo.db` 只包含虚构的展示数据，可安全用于截图、录屏和项目演示。

## 源码模式使用

在项目根目录执行：

```powershell
Copy-Item demo\task_records.demo.db task_records.db
python app.py
```

## Windows 发布版使用

将 `task_records.demo.db` 复制到 `LeyLineBook.exe` 旁边，并重命名为 `task_records.db`，然后启动程序。

不要用演示数据库覆盖自己的真实数据库。重新生成演示数据可执行：

```powershell
python scripts\create_demo_database.py
```

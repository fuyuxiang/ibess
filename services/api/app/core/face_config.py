"""人脸核验规则配置的种子行（单行表 id=1）。

这张表此前既没有模型定义也没有迁移，只在既有部署的库里手工建过。
全新环境部署时缺表，表现为：
  - 启动后周期性打印「自动比对周期异常：ProgrammingError」
  - 「规则配置」页取配置直接 500（核验配置缺失）
现在模型已进 app.models，create_all 会建表；种子由本模块补齐。

阈值用余弦口径（与 routers/faces.py 的判定、face_auto 的比对一致），
不要沿用建表 SQL 里 0.850/0.600 那套旧百分比口径。
"""
from sqlalchemy import text
from sqlalchemy.orm import Session

DEFAULT_THRESHOLD_SAME = 0.363
DEFAULT_THRESHOLD_SUSPECT = 0.280
DEFAULT_INTERVAL_SECONDS = 600      # 10 分钟：对外按分钟配置，库里仍以秒存


def ensure_face_config_seed(engine):
    """表建好后调用；已有配置行则原样保留，不覆盖使用方改过的阈值。"""
    with Session(engine) as session:
        session.execute(
            text(
                "insert into face_verify_config "
                "(id, threshold_same, threshold_suspect, auto_enabled, interval_seconds, updated_by) "
                "values (1, :same, :suspect, false, :interval, 'startup-seed') on conflict (id) do nothing"
            ),
            {
                "same": DEFAULT_THRESHOLD_SAME,
                "suspect": DEFAULT_THRESHOLD_SUSPECT,
                "interval": DEFAULT_INTERVAL_SECONDS,
            },
        )
        session.commit()

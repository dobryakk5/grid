# Выкладка

```bash
ssh ultra 'cd /var/py/grid/ && git pull && systemctl try-restart mini-grid-api mini-grid-worker mini-grid-dex-worker mini-grid-dex-sampler mini-grid-chain-tape mini-grid-fomo-registry'
```

Перечислены **все** сервисы, исполняющие код проекта, а не только два.
`mini-grid-worker` — это CEX-сетка (`app.workers.grid`); DEX-уровнями занимается
отдельный `mini-grid-dex-worker` (`app.workers.dex`). Пока он не был в этом
списке, правки в исполнении сделок выкладывались на диск и не доезжали до
процесса: API отвечал уже по-новому, а уровни исполнялись по коду недельной
давности — и отладка уходила в код, где всё было правильно.

`try-restart`, а не `restart`: он трогает только те юниты, что уже запущены, и
не поднимает намеренно остановленные (сэмплер и реестр FOMO включены не всегда).

Проверить, что все процессы действительно новые:

```bash
ssh ultra 'systemctl show mini-grid-api mini-grid-worker mini-grid-dex-worker mini-grid-chain-tape -p Names -p ActiveEnterTimestamp'
```

Время старта каждого должно быть позже времени `git pull`.

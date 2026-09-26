# 每日英文

每天早上 7 點（台灣時間）自動抓一篇英文新聞，擷取導言與第一段，挑出較難的生字並附上字典解析。
網頁上可以把生字加入生字簿，用間隔重複（SRS）複習。全程不使用 AI、不消耗 token。

- 網站：https://beibeing1218.github.io/daily-english/
- 星期一到日：政治／國際（衛報）、商業／經濟（衛報）、科技（Ars Technica）、科學／健康（The Conversation）、環境／氣候（Yale E360）、文化／藝術（衛報）、評論（衛報）
- 生字簿存在瀏覽器的 localStorage，只在加入生字的那個瀏覽器看得到；請用「匯出備份」定期備份。

## 檔案

- `build.py`：產生當天內容（Python 標準庫，不用安裝套件）。`python build.py --force` 重做今天。
- `index.html`：整個網站（單一檔案）。
- `data/days/*.json`：每天的內容；`data/index.json`：存檔清單；`data/seen.json`：用過的字與文章。
- `lexicon/en_50k.txt`：英文詞頻表，來自 [hermitdave/FrequencyWords](https://github.com/hermitdave/FrequencyWords)（MIT）。

## 資料來源

- 字典：[Datamuse API](https://www.datamuse.com/api/)（釋義來自 Wiktionary）
- 中文：Google 翻譯（免費端點）
- 文章摘錄版權屬原媒體所有，網頁附原文連結。

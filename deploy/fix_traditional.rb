# fix_traditional.rb - convert Traditional characters left in the zh-cn
# language pack to Simplified, mechanically, for every Translation row.
# Why mechanical: the flagged rows are too many to screenshot one by one
# (and some render as boxes in the console font), but each Traditional
# character has exactly one Simplified counterpart in UI text, so a
# fixed mapping converts them safely. Placeholders (%s, %{...}) and
# code fragments are untouched: only the mapped characters change.
# Prints how many rows changed plus up to 20 before/after samples.
# Run inside the railsserver container:
#   bundle exec rails runner /tmp/fix_traditional.rb
# Console output is English-only on purpose.
TRAD_TO_SIMP = {
  '個' => '个', '發' => '发', '後' => '后', '為' => '为', '們' => '们',
  '說' => '说', '時' => '时', '動' => '动', '國' => '国', '過' => '过',
  '對' => '对', '會' => '会', '來' => '来', '這' => '这', '機' => '机',
  '樂' => '乐', '讓' => '让', '認' => '认', '證' => '证', '話' => '话',
  '車' => '车', '門' => '门', '開' => '开', '關' => '关', '電' => '电',
  '頭' => '头', '體' => '体', '邊' => '边', '長' => '长', '問' => '问',
  '間' => '间', '與' => '与', '興' => '兴', '學' => '学', '覺' => '觉',
  '見' => '见', '觀' => '观', '親' => '亲', '計' => '计', '設' => '设',
  '議' => '议', '論' => '论', '運' => '运', '還' => '还', '進' => '进',
  '遠' => '远', '選' => '选', '錄' => '录', '錶' => '表', '灣' => '湾',
  '網' => '网', '線' => '线', '總' => '总', '縱' => '纵', '錢' => '钱',
  '錯' => '错', '醫' => '医', '聲' => '声', '處' => '处', '號' => '号',
  '裏' => '里', '製' => '制', '複' => '复', '價' => '价', '龍' => '龙',
  '龜' => '龟', '麗' => '丽', '慶' => '庆', '廣' => '广', '廠' => '厂',
  '廁' => '厕', '廂' => '厢', '慾' => '欲', '懲' => '惩', '懷' => '怀',
  '麼' => '么', '麽' => '么', '龐' => '庞', '願' => '愿', '應' => '应',
  '觸' => '触', '覽' => '览', '實' => '实', '際' => '际', '隨' => '随',
  '隱' => '隐', '顯' => '显', '響' => '响', '頁' => '页', '頂' => '顶',
  '頃' => '顷', '項' => '项', '順' => '顺', '須' => '须', '頸' => '颈',
  '題' => '题', '額' => '额', '顏' => '颜', '風' => '风', '飛' => '飞',
  '養' => '养', '餓' => '饿', '館' => '馆', '馬' => '马', '驚' => '惊',
  '驗' => '验', '髒' => '脏', '鬥' => '斗', '鬱' => '郁', '鬆' => '松',
  '靈' => '灵', '黨' => '党', '簡' => '简', '訊' => '讯', '語' => '语',
  '讀' => '读', '寫' => '写', '點' => '点', '數' => '数', '據' => '据',
  '庫' => '库', '術' => '术', '構' => '构', '備' => '备', '擇' => '择',
  '聯' => '联', '視' => '视', '圖' => '图', '標' => '标', '籤' => '签',
  '檢' => '检', '測' => '测', '試' => '试', '碼' => '码', '帳' => '账',
  '單' => '单', '報' => '报', '預' => '预', '權' => '权', '員' => '员',
  '組' => '组', '織' => '织', '臺' => '台', '寧' => '宁', '濟' => '济',
  '濱' => '滨', '漢' => '汉', '乾' => '干', '餘' => '余', '隻' => '只',
  '麵' => '面', '葉' => '叶', '曆' => '历', '歷' => '历', '聽' => '听',
  '讓' => '让', '護' => '护', '報' => '报', '擔' => '担', '據' => '据',
}.freeze

rows = Translation.where(locale: 'zh-cn')
scanned = 0
changed = 0
rows.find_each do |t|
  scanned += 1
  target = t.target.to_s
  next if target.empty?
  converted = target.chars.map { |ch| TRAD_TO_SIMP[ch] || ch }.join
  next if converted == target
  t.target = converted
  t.save!
  changed += 1
  if changed <= 20
    puts "FIXED | #{t.source.to_s[0, 50]} => #{target[0, 50]} => #{converted[0, 50]}"
  end
end
puts "scanned #{scanned} zh-cn rows, converted #{changed} targets"
begin
  Rails.cache.clear
  puts 'cache cleared'
rescue StandardError => e
  puts "cache clear FAILED: #{e.class}"
end
puts 'FIX_TRADITIONAL_DONE'

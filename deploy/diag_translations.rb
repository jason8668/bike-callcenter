# diag_translations.rb - systematic check of the zh-cn strings Zammad
# serves from its Translation table: counts rows, then lists targets
# that still look Traditional or English (up to 50 each, truncated).
# Read-only: changes nothing. Send the output back and overrides will be
# written for exactly the flagged strings.
# NOTE: if the row count is tiny (only the custom overrides), the base
# language pack is NOT in this table and the remaining strings must be
# caught by screenshot instead.
#   bundle exec rails runner /tmp/diag_translations.rb
# Console output is English-only on purpose.
TRADITIONAL = '個發後為們說時動國過對會來這機樂讓認證話車門開關電頭體邊長問間與興學覺見觀親計設議論運還進遠選錄錶灣網線總縱錢錯醫聲處號裏製複價龍龜麗慶廣廠廁廂慾懲懷'
rows = Translation.where(locale: 'zh-cn')
puts "zh-cn translation rows: #{rows.count}"
trad = []
english = []
rows.find_each do |t|
  target = t.target.to_s
  next if target.empty?
  trad << t if TRADITIONAL.chars.any? { |ch| target.include?(ch) }
  # Phrase-like ASCII English (a sentence, not a brand/code/URL).
  if target.match?(/\A[\x00-\x7F]+\z/) && target.match?(/[a-z]/) &&
     target.include?(' ') && !target.include?('http')
    english << t
  end
end
puts "traditional-looking targets: #{trad.size}"
trad.first(50).each { |t| puts "TRAD | #{t.source.to_s[0, 70]} => #{t.target.to_s[0, 70]}" }
puts "english-looking targets: #{english.size}"
english.first(50).each { |t| puts "EN | #{t.source.to_s[0, 70]} => #{t.target.to_s[0, 70]}" }
puts 'DIAG_TRANSLATIONS_DONE'

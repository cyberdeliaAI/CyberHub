# What is Danbooru?

Danbooru is a big online image board where pictures get labeled with short, structured tags. Every image gets tags that describe what's in it: the subject, colors, clothing, pose, background, and so on.

That way of labeling turned out to matter a lot for AI image generation. Plenty of anime and illustration models were trained on datasets full of Danbooru-style tags. So those models tend to understand prompts written in the same tag style better than they understand plain sentences.

A Danbooru prompt looks like this:

```text
1girl, solo, blue_eyes, long_hair, white_dress, looking_at_viewer, outdoors, soft_lighting
```

Instead of "a girl with blue eyes and long hair in a white dress standing outside," you use separate, recognizable tags.

---

## Why tags?

Tags have a few upsides over plain text:

- **Precise** — `blue_eyes` is clearer than "blue eyes."
- **Stackable** — you build up a full description from lots of small tags.
- **Fixed form** — every tag has a known spelling, so the model recognizes it.

---

## How do you write them?

A few simple rules:

- **Underscores** for terms with more than one word: `long_hair`, `looking_at_viewer`, `school_uniform`. (Lots of tools swap underscores for spaces while loading, so in practice the difference is often small.)
- **Commas** between tags: `blue_eyes, long_hair, smile`.
- **Lowercase**, unless the tag itself needs capitals.
- **Use real tags.** A made-up tag usually gets ignored or read as plain text by the model.

---

## Kinds of tags

Tags fall into categories. The main ones:

| Category | What it describes | Examples |
|---|---|---|
| General | visible things in the image | `smile`, `long_hair`, `sitting`, `city` |
| Character | named characters | `hatsune_miku` |
| Copyright | series, games, franchises | `pokemon`, `touhou` |
| Artist | artist/style references | artist names |
| Meta | quality, rating, era, technical labels | `highres`, `absurdres`, `newest` |

Most prompts are mainly built from **general** tags. Character, copyright, and artist tags only work well if the model knows them.

---

## What this toolkit does

The Danbooru Toolkit helps you use tags without memorizing exact spellings.

### Lookup

Search the tag database. Use this to find correct tag names, categories, and frequency.

### Builder

Build a comma-separated prompt from selected tags.

### Checker

Paste an existing prompt and check which tags are known or unknown.

### Random

Generate prompts from editable tag pools.

### Auto-Tag

Analyze an image with the optional ONNX model and turn predictions into tags.

---

## Practical workflow

For a new prompt:

1. Search tags in **Lookup**.
2. Add useful tags to **Builder**.
3. Arrange the prompt in a sensible order.
4. Copy it into your generator.

For cleaning an existing prompt:

1. Paste it into **Checker**.
2. Normalize the prompt.
3. Review unknown tags.
4. Replace or remove anything that does not belong.

For extracting tags from an image:

1. Open **Auto-Tag**.
2. Load the model.
3. Drop an image.
4. Analyze it.
5. Send useful tags to Builder or Checker.

---

## Prompt order

A simple order that usually works:

1. subject count: `1girl`, `solo`, `2girls`
2. character or source tags, if needed
3. body and identity details
4. clothing and accessories
5. expression and pose
6. camera and composition
7. background and lighting
8. quality or meta tags

Example:

```text
1girl, solo, long_hair, blue_eyes, white_dress, smile, looking_at_viewer, standing, garden, sunlight, depth_of_field, absurdres
```

---

## Common mistakes

- Writing long plain-English sentences when tags would be clearer.
- Using tags that are not in the database.
- Mixing too many style or artist tags.
- Repeating the same tag many times.
- Treating frequency as quality. Frequency means common, not automatically better.

Danbooru tags are best seen as a controlled vocabulary. They make prompts easier to search, clean, reuse, and compare.
